#!/usr/bin/env python3
"""
Samsung UCCM SCPI-to-NMEA Bridge
==================================
Connects via TCP to the SCPI-CLI of the Samsung UCCM GPS module
(port 2000), reads GPS data via SCPI commands, and generates
NMEA-0183 sentences for gpsd as well as NTP SHM timestamps for ntpd/chrony.

UCCM SCPI commands (queried from device):
  TIME:STRing?                       UTC time: "YYYY/MM/DD HH:MM:SS"
  GPS:POSition?                      Position: "N,+DD,+MM,+SS.sss,E,+DD,+MM,+SS.sss,+HHH.hh"
  GPS:SATellite:TRACking:COUNt?      Number of tracked satellites: "+N"
  GPS:SATellite:TRACking?            PRN list: "+prn,...,+prn"
  LED:GPSLock?                       Lock status: "Locked" | "Unlocked"
  SYNChronization:TFOMerit?          Time accuracy: "1 ~ 10 nsec"
  TOD EN / TOD DI                    1PPS TOD data stream on/off

TOD packets (hex-encoded in ASCII, 44 bytes per packet, one packet per second):
  Format: "XX XX XX ... XX \\r\\n"  (44 bytes = 132 characters incl. spaces)
  Byte  0:    0xC5 (sync)
  Byte 30:    BCD seconds (0x00..0x59)
  Other fields: proprietary Samsung format, not needed for timing.

Usage:
    ./uccm_scpi_bridge.py 172.16.20.30 2000
    gpsd -n /dev/uccm_gps
"""

import argparse
import ctypes
import ctypes.util
import fcntl
import json
import logging
import os
import pty
import re
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple

# Linux constants for serial DCD query
_TIOCMGET = getattr(termios, 'TIOCMGET', 0x5415)
_TIOCM_CD = 0x040  # DCD bit in modem status

# ---------------------------------------------------------------------------
# NMEA helper functions
# ---------------------------------------------------------------------------

def nmea_checksum(body: str) -> str:
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"{cs:02X}"

def build_nmea(*fields) -> bytes:
    body = ','.join(str(f) for f in fields)
    return f"${body}*{nmea_checksum(body)}\r\n".encode()

def deg_to_nmea_lat(deg: float) -> Tuple[str, str]:
    hemi = 'N' if deg >= 0 else 'S'
    deg  = abs(deg)
    d, m = int(deg), (deg - int(deg)) * 60
    return f"{d:02d}{m:09.6f}", hemi

def deg_to_nmea_lon(deg: float) -> Tuple[str, str]:
    hemi = 'E' if deg >= 0 else 'W'
    deg  = abs(deg)
    d, m = int(deg), (deg - int(deg)) * 60
    return f"{d:03d}{m:09.6f}", hemi


# ---------------------------------------------------------------------------
# NTP Shared Memory (type 28)
# ---------------------------------------------------------------------------

class ShmTime(ctypes.Structure):
    # Structure matches ntpd/refclock_shm.c without NSec fields (96 bytes on 64-bit).
    # The NSec fields were added in newer ntpd versions but are not present on
    # this system (ipcs shows 96 bytes for ntpd segments).
    # mode=0 → ntpd reads clockTimeStampSec/USec; NSec not needed.
    _fields_ = [
        ('mode',                   ctypes.c_int),
        ('count',                  ctypes.c_int),
        ('clockTimeStampSec',      ctypes.c_long),
        ('clockTimeStampUSec',     ctypes.c_int),
        ('receiveTimeStampSec',    ctypes.c_long),
        ('receiveTimeStampUSec',   ctypes.c_int),
        ('leap',                   ctypes.c_int),
        ('precision',              ctypes.c_int),
        ('nsamples',               ctypes.c_int),
        ('valid',                  ctypes.c_int),
        ('dummy',                  ctypes.c_int * 11),  # padding to 96 bytes
    ]


class NtpShm:
    IPC_CREAT = 0o1000
    SHM_KEYS  = [0x4e545030, 0x4e545031]

    def __init__(self, unit: int):
        self.unit    = unit
        self.shm_ptr = None
        self._libc   = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        self._libc.shmget.restype = ctypes.c_int
        self._libc.shmat.restype  = ctypes.c_void_p
        self._libc.shmdt.restype  = ctypes.c_int
        self._open()

    def _open(self):
        key    = self.SHM_KEYS[self.unit]
        shm_id = self._libc.shmget(
            ctypes.c_int(key),
            ctypes.c_size_t(ctypes.sizeof(ShmTime)),
            ctypes.c_int(self.IPC_CREAT | 0o666)
        )
        if shm_id < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        ptr = self._libc.shmat(ctypes.c_int(shm_id), None, ctypes.c_int(0))
        if ptr is None or ptr == ctypes.c_size_t(-1).value:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        self.shm_ptr = ctypes.cast(ctypes.c_void_p(ptr), ctypes.POINTER(ShmTime))
        logging.info(f"NTP SHM Unit {self.unit} opened (key=0x{key:08x})")

    def write(self, gps_time: datetime, recv_time: Optional[datetime] = None,
              precision: int = -1):
        if self.shm_ptr is None:
            return
        if recv_time is None:
            recv_time = datetime.now(timezone.utc)
        shm = self.shm_ptr.contents
        gps_e  = gps_time.timestamp()
        recv_e = recv_time.timestamp()
        gps_s, gps_us   = int(gps_e),  int((gps_e  - int(gps_e))  * 1e6)
        recv_s, recv_us = int(recv_e), int((recv_e - int(recv_e)) * 1e6)
        shm.valid = 0
        shm.count += 1                       # Odd = write in progress
        shm.clockTimeStampSec    = gps_s
        shm.clockTimeStampUSec   = gps_us
        shm.receiveTimeStampSec  = recv_s
        shm.receiveTimeStampUSec = recv_us
        shm.leap      = 0
        shm.precision = precision
        shm.mode      = 0
        shm.count    += 1                    # Even = write complete
        shm.valid     = 1

    def close(self):
        if self.shm_ptr is not None:
            self._libc.shmdt(ctypes.cast(self.shm_ptr, ctypes.c_void_p))
            self.shm_ptr = None


# ---------------------------------------------------------------------------
# PTY management
# ---------------------------------------------------------------------------

class GpsdPty:
    def __init__(self, symlink_path: str):
        self.symlink_path = symlink_path
        self.master_fd    = None
        self.slave_fd     = None

    def open(self):
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        # Raw mode
        attrs = termios.tcgetattr(slave_fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B4800
        attrs[5] = termios.B4800
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        # Symlink
        if os.path.islink(self.symlink_path) or os.path.exists(self.symlink_path):
            os.unlink(self.symlink_path)
        os.symlink(slave_path, self.symlink_path)
        self.master_fd = master_fd
        self.slave_fd  = slave_fd
        logging.info(f"PTY created: {slave_path} -> {self.symlink_path}")
        logging.info(f"Start gpsd with: gpsd -n {self.symlink_path}")

    def write(self, data: bytes):
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError as e:
                logging.warning(f"PTY write error: {e}")

    def close(self):
        for fd in (self.master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = self.slave_fd = None
        if os.path.islink(self.symlink_path):
            os.unlink(self.symlink_path)


# ---------------------------------------------------------------------------
# UCCM SCPI client with demultiplexer thread
# ---------------------------------------------------------------------------

import queue as _queue

# TOD packet: line starts with "c5 " followed by 43 more hex bytes.
# Note: UCCM-CLI may append the SCPI echo to the TOD line under concurrency
# (e.g. "c5 00 ... ca TIME:STRing?" length 144 instead of 131).
# Therefore: only check the start of the line; the rest is queued as SCPI.
_TOD_RE = re.compile(r'^(c5(?:\s+[0-9a-f]{2}){43})(.*)', re.IGNORECASE | re.DOTALL)

_SENTINEL = object()  # Queue termination sentinel


# ---------------------------------------------------------------------------
# Serial transport (alternative to TCP socket)
# ---------------------------------------------------------------------------

class _SerialTransport:
    """
    Serial transport for UccmScpiClient.
    Duck-type compatible with socket.socket (recv, sendall, fileno, close).
    Enables 1PPS detection via DCD pin (read_dcd).
    """
    _BAUD_CONSTS = {b: getattr(termios, f'B{b}', None)
                    for b in (1200, 2400, 4800, 9600, 19200, 38400,
                               57600, 115200, 230400)}

    def __init__(self, device: str, baud: int = 9600):
        self.device = device
        self.baud   = baud
        self.fd: Optional[int] = None

    def connect(self):
        baud_c = self._BAUD_CONSTS.get(self.baud)
        if baud_c is None:
            raise ValueError(f"Unknown baud rate: {self.baud}")
        self.fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY)
        attrs = list(termios.tcgetattr(self.fd))
        attrs[0] = termios.IGNBRK                          # iflag
        attrs[1] = 0                                       # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0                                       # lflag: no echo, non-canonical
        attrs[4] = baud_c                                  # ispeed
        attrs[5] = baud_c                                  # ospeed
        attrs[6][termios.VMIN]  = 1                        # block until 1 byte available
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        logging.info(f"Serial port opened: {self.device} @ {self.baud} baud")

    def recv(self, n: int) -> bytes:
        return os.read(self.fd, n)

    def sendall(self, data: bytes):
        total = 0
        while total < len(data):
            written = os.write(self.fd, data[total:])
            total += written

    def fileno(self) -> int:
        return self.fd

    def settimeout(self, _t):
        pass  # Serial: timeout is controlled via select

    def read_dcd(self) -> bool:
        """Returns True when DCD pin is asserted (1PPS pulse detected)."""
        buf = bytearray(4)
        fcntl.ioctl(self.fd, _TIOCMGET, buf)
        return bool(int.from_bytes(buf, sys.byteorder) & _TIOCM_CD)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class UccmScpiClient:
    """
    SCPI client with a single reader thread (demultiplexer).

    The reader thread continuously reads from the socket and distributes lines to:
      _scpi_queue  <- SCPI responses (ASCII text, "Command Complete")
      _tod_queue   <- TOD packets (hex-encoded binary packets, 44 bytes)

    query()           reads from _scpi_queue (thread-safe)
    recv_tod_packet() reads from _tod_queue (thread-safe)
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.host    = host
        self.port    = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._tod_enabled  = False
        self._scpi_queue: _queue.Queue = _queue.Queue()
        self._tod_queue:  _queue.Queue = _queue.Queue(maxsize=8)
        self._reader: Optional[threading.Thread] = None
        self._connected = False
        self._query_lock = threading.Lock()  # serializes concurrent query() calls

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self):
        """Establish TCP connection."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(None)  # reader thread blocks itself
        self._connected = True
        self._start_reader()
        self._drain_scpi(2.0)
        logging.info(f"SCPI connected to {self.host}:{self.port}")

    def connect_serial(self, transport: '_SerialTransport'):
        """Establish serial connection (alternative to TCP)."""
        transport.connect()
        self.sock = transport  # duck typing: recv/sendall/fileno/close
        self._connected = True
        self._start_reader()
        self._drain_scpi(2.0)
        logging.info(f"SCPI connected (serial) to {transport.device}")

    def _start_reader(self):
        self._reader = threading.Thread(target=self._reader_loop,
                                        name='scpi-reader', daemon=True)
        self._reader.start()

    def close(self):
        self._connected = False
        if self.sock:
            if self._tod_enabled:
                try:
                    self.sock.sendall(b'TOD DI\r\n')
                    time.sleep(0.1)
                except OSError:
                    pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        # Reader thread exits when socket is closed
        if self._reader:
            self._reader.join(timeout=2.0)
        # Terminate queues with sentinel
        self._scpi_queue.put(_SENTINEL)
        self._tod_queue.put(_SENTINEL)

    # ------------------------------------------------------------------
    # Demultiplexer thread (sole socket reader)
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """Reads all socket data, classifies lines, distributes to queues."""
        buf = b''
        try:
            while self._connected:
                # Blocking recv with timeout for clean shutdown
                ready = select.select([self.sock], [], [], 1.0)[0]
                if not ready:
                    continue
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                recv_time = datetime.now(timezone.utc)
                buf += chunk

                while b'\n' in buf:
                    line_b, buf = buf.split(b'\n', 1)
                    line = line_b.decode('ascii', errors='replace').strip()
                    if not line:
                        continue
                    m = _TOD_RE.match(line)
                    if m:
                        # Group 1: the 44 hex bytes (131 characters)
                        tod_hex  = m.group(1)
                        leftover = m.group(2).strip()
                        raw = bytes(int(h, 16) for h in tod_hex.split())
                        try:
                            self._tod_queue.put_nowait((raw, recv_time))
                        except _queue.Full:
                            try:
                                self._tod_queue.get_nowait()
                            except _queue.Empty:
                                pass
                            self._tod_queue.put_nowait((raw, recv_time))
                        # Forward any SCPI remainder (command echo)
                        if leftover:
                            self._scpi_queue.put(leftover)
                    else:
                        self._scpi_queue.put(line)
        except OSError:
            pass
        finally:
            self._connected = False
            self._scpi_queue.put(_SENTINEL)
            self._tod_queue.put(_SENTINEL)
            logging.debug("SCPI reader thread terminated")

    # ------------------------------------------------------------------
    # SCPI communication
    # ------------------------------------------------------------------

    def _drain_scpi(self, timeout: float):
        """Discards all pending SCPI lines.

        Stops as soon as the queue has been idle for 300 ms, but never
        waits longer than `timeout` seconds in total.
        """
        deadline     = time.time() + timeout
        idle_deadline = time.time() + 0.3
        while time.time() < deadline and time.time() < idle_deadline:
            try:
                item = self._scpi_queue.get(timeout=0.05)
                if item is _SENTINEL:
                    self._scpi_queue.put(_SENTINEL)
                    return
                idle_deadline = time.time() + 0.3  # reset on each item
            except _queue.Empty:
                pass

    def _collect_scpi_response(self, cmd: str, timeout: float) -> str:
        """
        Reads SCPI lines from the queue until 'Command Complete' appears.
        Returns cleaned response text.
        """
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                item = self._scpi_queue.get(timeout=remaining)
            except _queue.Empty:
                break
            if item is _SENTINEL:
                self._scpi_queue.put(_SENTINEL)
                raise ConnectionError("Connection closed")
            line = item
            if '"Command Complete"' in line:
                break
            # Filter out echo and prompt
            if line == cmd.strip():
                continue
            if line.startswith('UCCM>') or line == '':
                continue
            lines.append(line)
        return '\n'.join(lines).strip()

    def query(self, cmd: str, timeout: float = 3.0) -> str:
        """Sends SCPI command and returns response (thread-safe via queue)."""
        with self._query_lock:
            # Discard stale lines from a previous timed-out command.
            # If anything is found, wait until the queue has been idle for
            # 200 ms so in-flight socket data also gets discarded before we
            # send the new command.
            found_stale = False
            while True:
                try:
                    item = self._scpi_queue.get_nowait()
                    if item is _SENTINEL:
                        self._scpi_queue.put(_SENTINEL)
                        break
                    logging.debug(f"Discarding stale SCPI data before {cmd!r}: {item!r}")
                    found_stale = True
                except _queue.Empty:
                    break
            if found_stale:
                idle_deadline = time.time() + 0.2
                while time.time() < idle_deadline:
                    try:
                        item = self._scpi_queue.get(timeout=0.05)
                        if item is _SENTINEL:
                            self._scpi_queue.put(_SENTINEL)
                            break
                        logging.debug(f"Discarding stale SCPI data before {cmd!r}: {item!r}")
                        idle_deadline = time.time() + 0.2  # reset on each item
                    except _queue.Empty:
                        pass
            self.sock.sendall((cmd + '\r\n').encode())
            return self._collect_scpi_response(cmd, timeout)

    # ------------------------------------------------------------------
    # TOD data stream
    # ------------------------------------------------------------------

    def tod_enable(self):
        """Enables the TOD data stream."""
        self.sock.sendall(b'TOD EN\r\n')
        # Wait for acknowledgement
        self._collect_scpi_response('TOD EN', timeout=2.0)
        self._tod_enabled = True
        logging.info("TOD data stream enabled (1PPS packets incoming)")

    def tod_disable(self):
        self.sock.sendall(b'TOD DI\r\n')
        self._collect_scpi_response('TOD DI', timeout=2.0)
        self._tod_enabled = False

    def recv_tod_packet(self, timeout: float = 2.0) -> Optional[Tuple[bytes, datetime]]:
        """
        Waits for the next TOD packet from the queue.
        Returns (raw_bytes, recv_time) or None on timeout/error.
        """
        try:
            item = self._tod_queue.get(timeout=timeout)
            if item is _SENTINEL:
                self._tod_queue.put(_SENTINEL)
                return None
            return item
        except _queue.Empty:
            return None


# ---------------------------------------------------------------------------
# SCPI response parsers
# ---------------------------------------------------------------------------

def parse_time_string(resp: str) -> Optional[datetime]:
    """Parses "YYYY/MM/DD HH:MM:SS" -> datetime (UTC)."""
    m = re.search(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})', resp)
    if not m:
        return None
    Y, Mo, D, H, Mi, S = (int(x) for x in m.groups())
    try:
        return datetime(Y, Mo, D, H, Mi, S, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_position(resp: str) -> Optional[Tuple[float, float, float]]:
    """
    Parses "N,+49,+18,+51.668,E,+6,+56,+21.080,+315.12"
    -> (lat_deg, lon_deg, alt_m)
    """
    m = re.search(
        r'([NS]),\s*([+-]?\d+),\s*([+-]?\d+),\s*([+-]?[\d.]+),'
        r'\s*([EW]),\s*([+-]?\d+),\s*([+-]?\d+),\s*([+-]?[\d.]+),'
        r'\s*([+-]?[\d.]+)',
        resp
    )
    if not m:
        return None
    ns, lat_d, lat_m, lat_s, ew, lon_d, lon_m, lon_s, alt = m.groups()
    lat = float(lat_d) + float(lat_m) / 60 + float(lat_s) / 3600
    if ns == 'S':
        lat = -lat
    lon = float(lon_d) + float(lon_m) / 60 + float(lon_s) / 3600
    if ew == 'W':
        lon = -lon
    return lat, lon, float(alt)


def parse_sat_count(resp: str) -> int:
    m = re.search(r'[+-]?(\d+)', resp)
    return int(m.group(1)) if m else 0


def parse_gps_lock(resp: str) -> bool:
    return 'locked' in resp.lower()


def parse_prn_list(resp: str) -> list:
    """Parses "+12,+18,+22,..." -> [12, 18, 22, ...]"""
    return [int(m) for m in re.findall(r'[+-]?(\d+)', resp)]


def parse_system_status(resp: str) -> dict:
    """Extracts ANT voltage, current, and temperature from SYSTEM:STATUS? response."""
    result = {}
    m = re.search(r'ANT V=([\d.]+V),\s*I=([\d.]+mA)', resp)
    if m:
        result['ant_voltage'] = m.group(1)
        result['ant_current'] = m.group(2)
    m = re.search(r'Temp\s*=\s*([\d.]+)', resp)
    if m:
        result['temperature'] = m.group(1) + ' \u00b0C'
    return result


def parse_tod_seconds_bcd(pkt: bytes) -> Optional[int]:
    """Extracts seconds (BCD) from byte 30 of the TOD packet."""
    if len(pkt) < 31 or pkt[0] != 0xC5:
        return None
    bcd = pkt[30]
    tens = (bcd >> 4) & 0x0F
    ones = bcd & 0x0F
    if tens > 5 or ones > 9:
        return None
    return tens * 10 + ones


# ---------------------------------------------------------------------------
# NMEA generator
# ---------------------------------------------------------------------------

class NmeaGenerator:
    def __init__(self):
        self.lat = 0.0
        self.lon = 0.0
        self.alt = 0.0
        self.num_sats = 0
        self.locked   = False
        self.prns: list = []
        self._lock    = threading.Lock()

    def update_position(self, lat: float, lon: float, alt: float):
        with self._lock:
            self.lat, self.lon, self.alt = lat, lon, alt

    def update_status(self, num_sats: int, locked: bool):
        with self._lock:
            self.num_sats = num_sats
            self.locked   = locked

    def update_satellites(self, prns: list):
        with self._lock:
            self.prns = prns[:]

    def generate(self, now: datetime) -> list:
        """Generates NMEA sentences for the given UTC timestamp."""
        with self._lock:
            lat, lon, alt = self.lat, self.lon, self.alt
            sats   = self.num_sats
            locked = self.locked
            prns   = self.prns[:]

        status   = 'A' if locked else 'V'
        fix_qual = '1' if locked else '0'
        fix_type = '3' if locked else '1'  # GPGSA: 1=no fix, 2=2D fix, 3=3D fix

        tstr = f"{now.hour:02d}{now.minute:02d}{now.second:02d}.00"
        dstr = f"{now.day:02d}{now.month:02d}{now.year % 100:02d}"

        lat_s, lat_h = deg_to_nmea_lat(lat)
        lon_s, lon_h = deg_to_nmea_lon(lon)

        sentences = []

        # $GPRMC  (cycle starter per gpsd)
        sentences.append(build_nmea(
            'GPRMC', tstr, status,
            lat_s, lat_h, lon_s, lon_h,
            '0.000', '0.00', dstr, '', '', 'A'
        ))

        # $GPZDA  (most precise time source for NTP; must come before GPGGA
        #          because gpsd treats GPGGA as the cycle ender)
        sentences.append(build_nmea(
            'GPZDA', tstr,
            f"{now.day:02d}", f"{now.month:02d}", f"{now.year:04d}",
            '00', '00'
        ))

        # $GPGSA  (active satellites + fix type; gpsmon shows PRN list)
        # Up to 12 PRNs; fill remaining fields with empty strings
        prn_fields = [f"{p:02d}" for p in prns[:12]] + [''] * max(0, 12 - len(prns))
        sentences.append(build_nmea('GPGSA', 'A', fix_type, *prn_fields, '', '', ''))

        # $GPGSV is intentionally not generated:
        # The UCCM SCPI interface provides no azimuth/elevation/SNR data.
        # gpsd 3.25 checks whether at least one azimuth != 0 on the last GPGSV
        # message (SiRFstar workaround, driver_nmea0183.c:2296).
        # With all azimuths = 0 (atoi("") = 0) gpsd logs
        # "Satellite data no good" and discards the data.
        # GPGSA provides the PRN list for gpsmon without this error.

        # $GPGGA  (cycle ender per gpsd)
        sentences.append(build_nmea(
            'GPGGA', tstr,
            lat_s, lat_h, lon_s, lon_h,
            fix_qual, f"{sats:02d}",
            '1.0', f"{alt:.2f}", 'M', '47.0', 'M', '', ''
        ))

        return sentences


# ---------------------------------------------------------------------------
# Bridge status (thread-safe, for web interface)
# ---------------------------------------------------------------------------

class BridgeStatus:
    """Thread-safe status store for the web interface."""

    def __init__(self, pps_source_cfg: str):
        self._lock = threading.Lock()
        self._data = {
            'connected':    False,
            'gps_locked':   False,
            'num_sats':     0,
            'prns':         [],
            'lat':          None,
            'lon':          None,
            'alt':          None,
            'last_gps_time': None,   # ISO-String
            'last_pps_time': None,   # ISO-String
            'tfom':         '',
            'ant_voltage':  None,
            'ant_current':  None,
            'temperature':  None,
            'pps_source':   pps_source_cfg,
            'started_at':   datetime.now(timezone.utc).isoformat(),
        }

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


# ---------------------------------------------------------------------------
# Web-Interface
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UCCM GPS Bridge</title>
<style>
  body { font-family: monospace; background: #111; color: #ccc; margin: 2em; }
  h1   { color: #0af; }
  h2   { color: #0af; font-size: 1em; margin-top: 2em; }
  table { border-collapse: collapse; margin-top: 1em; }
  td, th { padding: 0.3em 1.2em 0.3em 0; text-align: left; }
  th   { color: #888; font-weight: normal; }
  .ok  { color: #4c4; }
  .err { color: #e44; }
  .warn{ color: #fa0; }
  #ts  { color: #555; font-size: 0.85em; margin-top: 1.5em; }
  .log-btns { margin-top: 1.5em; }
  .log-btns button { background: #222; color: #ccc; border: 1px solid #555;
    padding: 0.3em 1em; cursor: pointer; margin-right: 0.5em; font-family: monospace; }
  .log-btns button:hover { background: #333; }
  .log-btns button.danger { border-color: #e44; color: #e44; }
  .log-btns button.danger:hover { background: #300; }
  #log-out { margin-top: 0.8em; white-space: pre-wrap; background: #1a1a1a;
    border: 1px solid #333; padding: 0.8em; max-height: 20em; overflow-y: auto;
    font-size: 0.9em; display: none; }
  #log-status { color: #555; font-size: 0.85em; margin-top: 0.4em; }
</style>
</head>
<body>
<h1>Samsung UCCM GPS Bridge</h1>
<table id="tbl"><tr><td>Loading...</td></tr></table>
<div id="ts"></div>

<h2>Diagnostic Log</h2>
<div class="log-btns">
  <button onclick="loadLog()">Load Log</button>
  <button class="danger" onclick="clearLog()">Clear Log</button>
</div>
<div id="log-status"></div>
<pre id="log-out"></pre>

<script>
function fmt(v) { return v === null || v === '' ? '\u2013' : v; }
async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    const rows = [
      ['Connection',   d.connected   ? '<span class="ok">Connected</span>' : '<span class="err">Disconnected</span>'],
      ['GPS Lock',     d.gps_locked  ? '<span class="ok">Locked</span>'    : '<span class="err">Unlocked</span>'],
      ['GPS Time',     fmt(d.last_gps_time)],
      ['Position',     d.lat !== null ? `${d.lat.toFixed(6)} / ${d.lon.toFixed(6)} / ${d.alt.toFixed(1)} m` : '\u2013'],
      ['Satellites',   d.num_sats + (d.prns.length ? ' (PRNs: ' + d.prns.join(', ') + ')' : '')],
      ['TFOM',         fmt(d.tfom)],
      ['ANT Voltage',  fmt(d.ant_voltage)],
      ['ANT Current',  fmt(d.ant_current)],
      ['Temperature',  fmt(d.temperature)],
      ['1PPS Source',  fmt(d.pps_source)],
      ['Last PPS',     fmt(d.last_pps_time)],
      ['Bridge Start', fmt(d.started_at)],
    ];
    document.getElementById('tbl').innerHTML =
      rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('');
    document.getElementById('ts').textContent =
      'Updated: ' + new Date().toISOString();
  } catch(e) {
    document.getElementById('ts').textContent = 'Error: ' + e;
  }
}

async function loadLog() {
  const st = document.getElementById('log-status');
  const out = document.getElementById('log-out');
  st.textContent = 'Loading log...';
  try {
    const r = await fetch('/log');
    const d = await r.json();
    if (d.error) { st.textContent = 'Error: ' + d.error; return; }
    out.textContent = d.lines.length ? d.lines.join('\\n') : '(Log is empty)';
    out.style.display = 'block';
    st.textContent = d.lines.length + ' entries \u2013 ' + new Date().toISOString();
  } catch(e) {
    st.textContent = 'Error: ' + e;
  }
}

async function clearLog() {
  if (!confirm('Really clear the diagnostic log?')) return;
  const st = document.getElementById('log-status');
  st.textContent = 'Clearing log...';
  try {
    const r = await fetch('/log/clear', {method: 'POST'});
    const d = await r.json();
    if (d.error) { st.textContent = 'Error: ' + d.error; return; }
    document.getElementById('log-out').textContent = '';
    document.getElementById('log-out').style.display = 'none';
    st.textContent = 'Log cleared \u2013 ' + new Date().toISOString();
  } catch(e) {
    st.textContent = 'Error: ' + e;
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class _WebHandler(BaseHTTPRequestHandler):
    """HTTP handler for bridge status."""

    # Class attributes set by WebServer
    status: 'BridgeStatus' = None
    get_client      = None  # Callable[[], Optional[UccmScpiClient]]
    make_log_client = None  # Callable[[], UccmScpiClient] – creates a dedicated log connection

    def _json_response(self, data, code: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_log_query(self, cmd: str, timeout: float) -> str:
        """Execute a log SCPI command on a dedicated short-lived connection.

        Using a separate connection keeps log operations completely
        independent of the main polling client so TIME:STRing? continues
        to run without interruption and NTP SHM timestamps stay current.
        Falls back to the main client if the device refuses a second
        connection.
        """
        if self.make_log_client is not None:
            try:
                lc = self.make_log_client()
                lc.connect()
                try:
                    return lc.query(cmd, timeout=timeout)
                finally:
                    lc.disconnect()
            except Exception:
                # Device may not accept a second connection (serial mode,
                # single-client TCP, …) – fall back to the main client.
                logging.debug("Log connection failed, falling back to main client",
                              exc_info=True)
        main = self.get_client()
        if main is None:
            raise ConnectionError("Not connected")
        return main.query(cmd, timeout=timeout)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/status':
            self._json_response(self.status.snapshot())
        elif path == '/log':
            if self.get_client() is None:
                self._json_response({'error': 'Not connected'}, 503)
                return
            try:
                raw = self._run_log_query('DIAGNOSTIC:LOG:READ:ALL?', timeout=10.0)
                lines = [l for l in raw.splitlines()
                         if l.strip() and not l.startswith(('infohead[', 'head[', 'Log count ='))]
                self._json_response({'lines': lines})
            except Exception as e:
                self._json_response({'error': str(e)}, 500)
        elif path in ('/', '/index.html'):
            body = _HTML_TEMPLATE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/log/clear':
            if self.get_client() is None:
                self._json_response({'error': 'Not connected'}, 503)
                return
            try:
                self._run_log_query('DIAGNOSTIC:LOG:CLEAR', timeout=5.0)
                self._json_response({'ok': True})
            except Exception as e:
                self._json_response({'error': str(e)}, 500)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):  # noqa: A002
        logging.debug(f"HTTP {self.address_string()} {fmt % args}")


class WebServer:
    """Starts an HTTP server in a daemon thread."""

    def __init__(self, port: int, status: BridgeStatus, get_client,
                 make_log_client=None):
        self.port            = port
        self.status          = status
        self.get_client      = get_client       # Callable[[], Optional[UccmScpiClient]]
        self.make_log_client = make_log_client  # Callable[[], UccmScpiClient]
        self._srv: Optional[HTTPServer] = None

    def start(self):
        # Derive handler class per instance so that status is set cleanly
        handler = type('_H', (_WebHandler,),
                       {'status':          self.status,
                        'get_client':      staticmethod(self.get_client),
                        'make_log_client': staticmethod(self.make_log_client)})
        self._srv = HTTPServer(('', self.port), handler)
        t = threading.Thread(target=self._srv.serve_forever,
                             name='web', daemon=True)
        t.start()
        logging.info(f"Web interface: http://0.0.0.0:{self.port}/")

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv = None


# ---------------------------------------------------------------------------
# Main bridge
# ---------------------------------------------------------------------------

class UccmScpiBridge:
    """
    Bridges UCCM SCPI -> gpsd PTY + NTP SHM.

    Architecture (single-thread polling + optional TOD thread):
      Main thread: SCPI polling (TIME:STRing?, GPS:POSition?, ...) @ 1 Hz
      TOD thread:  TOD packet reception for precise 1PPS timing (optional)
    """

    def __init__(self, args):
        self.host            = args.host
        self.port            = args.port
        self.serial_device   = args.serial       # None → TCP mode
        self.serial_baud     = args.baud
        self.pty_path        = args.pty
        self.reconnect_delay = args.reconnect_delay
        self.use_shm         = args.ntp_shm
        # --no-tod is an alias for --pps-source none (backwards compatibility)
        self.pps_source      = 'none' if args.no_tod else args.pps_source
        self.web_port        = args.web_port
        self.running         = False
        self._pty: Optional[GpsdPty]       = None
        self._shm0: Optional[NtpShm]       = None
        self._shm1: Optional[NtpShm]       = None
        self._nmea                         = NmeaGenerator()
        self._last_pos_time                = 0.0
        self._tod_client: Optional[UccmScpiClient] = None
        self._scpi_client: Optional[UccmScpiClient] = None  # aktiver Client fuer Web-API
        self._status                       = BridgeStatus(self.pps_source)
        self._web: Optional[WebServer]     = None

    # --- Lifecycle ----------------------------------------------------------

    def start(self):
        self.running = True
        self._pty = GpsdPty(self.pty_path)
        self._pty.open()

        if self.use_shm:
            for unit, attr in [(0, '_shm0'), (1, '_shm1')]:
                try:
                    shm = NtpShm(unit=unit)
                    setattr(self, attr, shm)
                except OSError as e:
                    logging.warning(f"NTP SHM Unit {unit} not available: {e}")

        if self.web_port:
            # A separate log connection is only possible in TCP mode.
            # In serial mode make_log_client stays None and log queries
            # fall back to the shared main client.
            make_log_client = (
                (lambda: UccmScpiClient(self.host, self.port))
                if not self.serial_device else None
            )
            self._web = WebServer(
                self.web_port, self._status,
                get_client=lambda: self._scpi_client,
                make_log_client=make_log_client,
            )
            self._web.start()

        self._thread = threading.Thread(target=self._main_loop,
                                        name='scpi', daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._web:
            self._web.stop()
        if self._pty:
            self._pty.close()
        for shm in (self._shm0, self._shm1):
            if shm:
                shm.close()

    def wait(self):
        self._thread.join()

    # --- Main loop ----------------------------------------------------------

    def _main_loop(self):
        while self.running:
            client = self._connect_scpi()
            if client is None:
                break
            try:
                self._scpi_client = client
                self._run_session(client)
            except Exception as e:
                logging.error(f"Session error: {e}")
            finally:
                self._scpi_client = None
                client.close()
                self._status.update(connected=False)
            if self.running:
                logging.info(f"Connection lost. Reconnecting in "
                             f"{self.reconnect_delay}s...")
                time.sleep(self.reconnect_delay)

    def _connect_scpi(self) -> Optional[UccmScpiClient]:
        while self.running:
            try:
                if self.serial_device:
                    logging.info(f"Connecting SCPI via serial: {self.serial_device} "
                                 f"@ {self.serial_baud} baud...")
                    c = UccmScpiClient(self.serial_device, 0)
                    t = _SerialTransport(self.serial_device, self.serial_baud)
                    c.connect_serial(t)
                else:
                    logging.info(f"Connecting SCPI to {self.host}:{self.port}...")
                    c = UccmScpiClient(self.host, self.port)
                    c.connect()
                self._status.update(connected=True)
                return c
            except OSError as e:
                self._status.update(connected=False)
                logging.warning(f"Connection failed: {e}. "
                                f"Retry in {self.reconnect_delay}s...")
                time.sleep(self.reconnect_delay)
        return None

    # --- Session ------------------------------------------------------------

    def _run_session(self, client: UccmScpiClient):
        """Runs a SCPI session: initial setup + polling loop."""

        # Identify device
        idn = client.query('*IDN?', timeout=3)
        logging.info(f"UCCM IDN: {idn.strip()!r}")

        # Fetch initial position
        self._update_position(client)
        self._update_status(client)

        # 1PPS thread: determine source via --pps-source
        # 'auto': TCP→TOD, serial→DCD  |  'tod': TOD  |  'dcd': DCD  |  'none': no 1PPS
        pps_thread = None
        effective_pps = self.pps_source
        if effective_pps == 'auto':
            effective_pps = 'dcd' if self.serial_device else 'tod'
        if effective_pps == 'dcd' and not self.serial_device:
            logging.warning("--pps-source=dcd requires --serial; 1PPS disabled")
            effective_pps = 'none'

        if effective_pps != 'none' and self._shm1:
            if effective_pps == 'dcd':
                logging.info("1PPS source: DCD pin")
                pps_thread = threading.Thread(
                    target=self._dcd_loop, args=(client.sock,),
                    name='dcd-pps', daemon=True
                )
            else:  # tod
                logging.info("1PPS source: TOD data stream")
                client.tod_enable()
                self._tod_client = client
                pps_thread = threading.Thread(
                    target=self._tod_loop, args=(client,),
                    name='tod', daemon=True
                )
            pps_thread.start()

        # Polling loop: TIME:STRing? once per second + periodic GPS data
        poll_cycle  = 0
        last_second = -1

        while self.running:
            t_start = time.time()

            # Query time
            try:
                resp = client.query('TIME:STRing?', timeout=3)
            except Exception as e:
                raise ConnectionError(f"TIME:STRing? failed: {e}")

            recv_time = datetime.now(timezone.utc)
            gps_time  = parse_time_string(resp)

            if gps_time is None:
                logging.warning(f"Invalid TIME:STRing response: {resp!r}")
                time.sleep(0.5)
                continue

            # Generate NMEA only once per second
            if gps_time.second != last_second:
                last_second = gps_time.second
                logging.debug(f"GPS time: {gps_time.isoformat()} "
                              f"(received: {recv_time.isoformat()})")
                self._status.update(last_gps_time=gps_time.isoformat())

                # Generate NMEA and write to PTY
                sentences = self._nmea.generate(gps_time)
                for s in sentences:
                    self._pty.write(s)
                    logging.debug(f"NMEA: {s.decode().strip()}")

                # NTP SHM0 (coarse GPS second reference)
                # When TOD is active, SHM0 is kept by the TOD thread which
                # has a stable, small offset (~50 ms TOD-packet latency).
                # Without TOD, use gps_time as both timestamps so the
                # reported offset is 0 (honest to ±0.5 s precision) rather
                # than -(variable SCPI latency) which causes NTP falseticker.
                if self._shm0 and self._tod_client is None:
                    self._shm0.write(gps_time, gps_time, precision=-1)
                    logging.debug(f"NTP SHM0: {gps_time.isoformat()}")

            # Periodic queries (every 30th cycle)
            poll_cycle += 1
            if poll_cycle % 30 == 0 or poll_cycle == 1:
                try:
                    self._update_position(client)
                    self._update_status(client)
                except Exception as e:
                    logging.warning(f"Periodic query failed: {e}")

            # Maintain exactly 1 second interval
            elapsed = time.time() - t_start
            sleep_s = max(0, 1.0 - elapsed)
            time.sleep(sleep_s)

        if pps_thread:
            pps_thread.join(timeout=2)

    def _tod_loop(self, client: UccmScpiClient):
        """
        Receives TOD packets in a separate thread.
        Uses the packet arrival time as the 1PPS reference.
        """
        logging.info("TOD thread started")
        while self.running and self._tod_client is client:
            result = client.recv_tod_packet(timeout=2.0)
            if result is None:
                continue
            pkt, recv_time = result
            sec_bcd = parse_tod_seconds_bcd(pkt)
            logging.debug(f"TOD packet received (second BCD="
                          f"{'None' if sec_bcd is None else f'{sec_bcd:#04x}'}), "
                          f"received: {recv_time.isoformat()}")

            # GPS second from TOD packet: use receive-time rounded to second.
            # The TOD packet arrives within ~50 ms of the PPS edge, so the
            # offset (gps_sec – recv_time ≈ –50 ms) is small and stable.
            gps_sec = recv_time.replace(microsecond=0)
            self._status.update(last_pps_time=recv_time.isoformat())
            # SHM0: coarse second reference (stable TOD latency, no SCPI jitter)
            if self._shm0:
                self._shm0.write(gps_sec, recv_time, precision=-1)
                logging.debug(f"NTP SHM0 (TOD): {gps_sec.isoformat()}")
            if self._shm1:
                self._shm1.write(gps_sec, recv_time, precision=-9)
                logging.debug(f"NTP SHM1 (1PPS): {gps_sec.isoformat()}")

        logging.info("TOD thread terminated")

    def _dcd_loop(self, transport: '_SerialTransport'):
        """
        1PPS via DCD pin of the serial port (serial mode).
        Detects rising DCD edge and writes timestamp to NTP SHM1.
        """
        logging.info("DCD 1PPS thread started")
        last_dcd = False
        while self.running:
            try:
                dcd = transport.read_dcd()
            except OSError as e:
                logging.warning(f"DCD read error: {e}")
                time.sleep(0.1)
                continue
            # Rising edge: DCD transitions from low to high
            if dcd and not last_dcd:
                recv_time = datetime.now(timezone.utc)
                gps_sec   = recv_time.replace(microsecond=0)
                self._status.update(last_pps_time=recv_time.isoformat())
                if self._shm1:
                    self._shm1.write(gps_sec, recv_time, precision=-9)
                    logging.debug(f"NTP SHM1 (DCD 1PPS): {gps_sec.isoformat()}")
            last_dcd = dcd
            time.sleep(0.001)  # 1 ms polling
        logging.info("DCD 1PPS thread terminated")

    # --- Helper methods -----------------------------------------------------

    def _update_position(self, client: UccmScpiClient):
        resp = client.query('GPS:POSition?', timeout=3)
        pos  = parse_position(resp)
        if pos:
            lat, lon, alt = pos
            self._nmea.update_position(lat, lon, alt)
            self._status.update(lat=round(lat, 7), lon=round(lon, 7), alt=round(alt, 2))
            logging.info(f"Position: {lat:+.6f} {lon:+.6f} alt={alt:.1f}m")
        else:
            logging.warning(f"Position not parseable: {resp!r}")

    def _update_status(self, client: UccmScpiClient):
        try:
            sats_resp = client.query('GPS:SATellite:TRACking:COUNt?', timeout=3)
            prn_resp  = client.query('GPS:SATellite:TRACking?', timeout=3)
            lock_resp = client.query('LED:GPSLock?', timeout=3)
            tfom_resp = client.query('SYNChronization:TFOMerit?', timeout=3)
            sys_resp  = client.query('SYSTEM:STATUS?', timeout=5)
            sats   = parse_sat_count(sats_resp)
            prns   = parse_prn_list(prn_resp)
            locked = parse_gps_lock(lock_resp)
            tfom   = tfom_resp.split('\n')[0].strip()
            sys_st = parse_system_status(sys_resp)
            self._nmea.update_status(sats, locked)
            self._nmea.update_satellites(prns)
            self._status.update(num_sats=sats, prns=prns, gps_locked=locked,
                                tfom=tfom, **sys_st)
            logging.info(f"Status: {sats} satellites, PRNs={prns}, lock={locked}, "
                         f"TFOM={tfom!r}, {sys_st}")
        except Exception as e:
            logging.warning(f"Status query failed: {e}")


# ---------------------------------------------------------------------------
# Signal handler & argument parser
# ---------------------------------------------------------------------------

def make_signal_handler(bridge: UccmScpiBridge):
    def handler(sig, frame):
        logging.info(f"Signal {sig} received, shutting down...")
        bridge.stop()
    return handler


def parse_args():
    parser = argparse.ArgumentParser(
        description='Samsung UCCM SCPI bridge: TCP/serial SCPI -> PTY (gpsd) + NTP SHM',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            'Connection (one required):\n'
            '  TCP:    HOST PORT                   (e.g. 172.16.20.30 2000)\n'
            '  Serial: --serial /dev/ttyUSB0\n'
            '\n'
            '1PPS source (--pps-source):\n'
            '  auto  TCP→TOD packets, serial→DCD pin  (default)\n'
            '  tod   TOD data stream (TCP and serial)\n'
            '  dcd   DCD pin         (serial only)\n'
            '  none  no 1PPS\n'
        )
    )
    # Transport: TCP (positional) or serial (--serial), mutually exclusive
    parser.add_argument('host', nargs='?', default=None,
                        help='Hostname/IP of the UCCM for TCP connection')
    parser.add_argument('port', nargs='?', type=int, default=None,
                        help='TCP port of the UCCM CLI (typically 2000)')
    parser.add_argument('--serial', metavar='DEVICE', default=None,
                        help='Serial device instead of TCP (e.g. /dev/ttyUSB0)')
    parser.add_argument('--baud', type=int, default=9600, metavar='BAUD',
                        help='Baud rate for serial device')
    parser.add_argument('--pty', default='/dev/uccm_gps',
                        help='PTY symlink path for gpsd')
    parser.add_argument('--reconnect-delay', type=float, default=5.0,
                        metavar='SEC')
    parser.add_argument('--ntp-shm', action='store_true',
                        help='Enable NTP SHM (Unit 0=NMEA, Unit 1=1PPS)')
    parser.add_argument('--pps-source', default='auto',
                        choices=['auto', 'tod', 'dcd', 'none'],
                        help='1PPS source: auto|tod|dcd|none')
    parser.add_argument('--no-tod', action='store_true',
                        help='Disable 1PPS (alias for --pps-source none)')
    parser.add_argument('--web-port', type=int, default=0, metavar='PORT',
                        help='HTTP status port (0 = disabled, e.g. 8080)')
    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args()

    # Validation: either TCP or serial, not both, not neither
    if args.serial:
        if args.host or args.port:
            parser.error('--serial and HOST/PORT are mutually exclusive')
    else:
        if not args.host or args.port is None:
            parser.error('provide HOST and PORT or use --serial DEVICE')
    return args


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    bridge = UccmScpiBridge(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, make_signal_handler(bridge))

    bridge.start()
    logging.info(f"Bridge running. PTY: {args.pty}")
    if args.web_port:
        logging.info(f"Web interface: http://localhost:{args.web_port}/")
    if args.ntp_shm:
        logging.info("NTP SHM active:")
        logging.info("  server 127.127.28.0 minpoll 4 maxpoll 4    # NMEA")
        logging.info("  fudge  127.127.28.0 refid GPS")
        logging.info("  server 127.127.28.1 minpoll 4 maxpoll 4 prefer  # TOD 1PPS")
        logging.info("  fudge  127.127.28.1 refid PPS")

    try:
        bridge.wait()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    logging.info("Bridge stopped.")


if __name__ == '__main__':
    main()

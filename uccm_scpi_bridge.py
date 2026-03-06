#!/usr/bin/env python3
"""
Samsung UCCM SCPI-zu-NMEA-Bridge
==================================
Verbindet sich per TCP mit der SCPI-CLI des Samsung UCCM GPS-Moduls
(Port 2000), liest GPS-Daten via SCPI-Befehle und erzeugt daraus
NMEA-0183-Saetze fuer gpsd sowie NTP-SHM-Zeitstempel fuer ntpd/chrony.

UCCM SCPI-Befehle (ermittelt vom Geraet):
  TIME:STRing?                       UTC-Zeit: "YYYY/MM/DD HH:MM:SS"
  GPS:POSition?                      Position: "N,+DD,+MM,+SS.sss,E,+DD,+MM,+SS.sss,+HHH.hh"
  GPS:SATellite:TRACking:COUNt?      Anzahl getrackter Satelliten: "+N"
  GPS:SATellite:TRACking?            PRN-Liste: "+prn,...,+prn"
  LED:GPSLock?                       Lock-Status: "Locked" | "Unlocked"
  SYNChronization:TFOMerit?          Zeitgenauigkeit: "1 ~ 10 nsec"
  TOD EN / TOD DI                    1PPS-TOD-Datenstrom an/aus

TOD-Pakete (hex-kodiert in ASCII, 44 Bytes pro Paket, ein Paket pro Sekunde):
  Format: "XX XX XX ... XX \\r\\n"  (44 Bytes = 132 Zeichen inkl. Leerzeichen)
  Byte  0:    0xC5 (Sync)
  Byte 30:    BCD-Sekunden (0x00..0x59)
  Andere Felder: proprietaeres Samsung-Format, fuer Timing nicht benoetigt.

Ausfuehren:
    ./uccm_scpi_bridge.py 172.16.20.30 2000
    gpsd -n /dev/uccm_gps
"""

import argparse
import ctypes
import ctypes.util
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
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# NMEA-Hilfsfunktionen
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
# NTP Shared Memory (Typ 28, identisch mit uccm_bridge.py)
# ---------------------------------------------------------------------------

class ShmTime(ctypes.Structure):
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
        ('clockTimeStampNSec',     ctypes.c_uint),
        ('receiveTimeStampNSec',   ctypes.c_uint),
        ('dummy',                  ctypes.c_int * 10),
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
        logging.info(f"NTP SHM Unit {self.unit} geoeffnet (key=0x{key:08x})")

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
        shm.count += 1
        shm.clockTimeStampSec    = gps_s
        shm.clockTimeStampUSec   = gps_us
        shm.clockTimeStampNSec   = gps_us * 1000
        shm.receiveTimeStampSec  = recv_s
        shm.receiveTimeStampUSec = recv_us
        shm.receiveTimeStampNSec = recv_us * 1000
        shm.leap      = 0
        shm.precision = precision
        shm.mode      = 0
        shm.count    += 1
        shm.valid     = 1

    def close(self):
        if self.shm_ptr is not None:
            self._libc.shmdt(ctypes.cast(self.shm_ptr, ctypes.c_void_p))
            self.shm_ptr = None


# ---------------------------------------------------------------------------
# PTY-Verwaltung (identisch mit uccm_bridge.py)
# ---------------------------------------------------------------------------

class GpsdPty:
    def __init__(self, symlink_path: str):
        self.symlink_path = symlink_path
        self.master_fd    = None
        self.slave_fd     = None

    def open(self):
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        # Raw-Modus
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
        logging.info(f"PTY erstellt: {slave_path} -> {self.symlink_path}")
        logging.info(f"gpsd starten mit: gpsd -n {self.symlink_path}")

    def write(self, data: bytes):
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError as e:
                logging.warning(f"PTY-Schreibfehler: {e}")

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
# UCCM SCPI-Client mit Demultiplexer-Thread
# ---------------------------------------------------------------------------

import queue as _queue

# TOD-Paket: Zeile beginnt mit "c5 " gefolgt von 43 weiteren Hex-Bytes.
# Achtung: UCCM-CLI haengt bei Gleichzeitigkeit das SCPI-Echo an die TOD-Zeile
# (z.B. "c5 00 ... ca TIME:STRing?" Laenge 144 statt 131).
# Deshalb: nur Zeilenbeginn pruefen, Rest wird separat als SCPI eingereiht.
_TOD_RE = re.compile(r'^(c5(?:\s+[0-9a-f]{2}){43})(.*)', re.IGNORECASE | re.DOTALL)

_SENTINEL = object()  # Queue-Abbruch-Signal


class UccmScpiClient:
    """
    SCPI-Client mit einem einzigen Lese-Thread (Demultiplexer).

    Der Reader-Thread liest kontinuierlich vom Socket und verteilt Zeilen auf:
      _scpi_queue  <- SCPI-Antworten (ASCII-Text, "Command Complete")
      _tod_queue   <- TOD-Pakete (hex-kodierte Binaerpakete, 44 Bytes)

    query()          liest aus _scpi_queue (Thread-sicher)
    recv_tod_packet() liest aus _tod_queue (Thread-sicher)
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

    # ------------------------------------------------------------------
    # Verbindung
    # ------------------------------------------------------------------

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(None)  # Reader-Thread blockiert selbst
        self._connected = True

        self._reader = threading.Thread(target=self._reader_loop,
                                        name='scpi-reader', daemon=True)
        self._reader.start()

        # Anfangs-Prompt abwarten
        self._drain_scpi(2.0)
        logging.info(f"SCPI-Verbunden mit {self.host}:{self.port}")

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
        # Reader-Thread beendet sich durch das geschlossene Socket
        if self._reader:
            self._reader.join(timeout=2.0)
        # Queues mit Sentinel abschliessen
        self._scpi_queue.put(_SENTINEL)
        self._tod_queue.put(_SENTINEL)

    # ------------------------------------------------------------------
    # Demultiplexer-Thread (einziger Socket-Leser)
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """Liest alle Socket-Daten, klassifiziert Zeilen, verteilt auf Queues."""
        buf = b''
        try:
            while self._connected:
                # Blockierender Recv mit Timeout fuer sauberes Beenden
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
                        # Gruppe 1: die 44 Hex-Bytes (131 Zeichen)
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
                        # Eventuelle SCPI-Reste (Befehlsecho) weiterleiten
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
            logging.debug("SCPI-Reader-Thread beendet")

    # ------------------------------------------------------------------
    # SCPI-Kommunikation
    # ------------------------------------------------------------------

    def _drain_scpi(self, timeout: float):
        """Verwirft alle aktuellen SCPI-Zeilen fuer `timeout` Sekunden."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                item = self._scpi_queue.get(timeout=0.1)
                if item is _SENTINEL:
                    self._scpi_queue.put(_SENTINEL)
                    break
            except _queue.Empty:
                pass

    def _collect_scpi_response(self, cmd: str, timeout: float) -> str:
        """
        Liest SCPI-Zeilen aus der Queue bis 'Command Complete' erscheint.
        Gibt bereinigten Antwort-Text zurueck.
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
                raise ConnectionError("Verbindung geschlossen")
            line = item
            if '"Command Complete"' in line:
                break
            # Echo und Prompt herausfiltern
            if line == cmd.strip():
                continue
            if line.startswith('UCCM>') or line == '':
                continue
            lines.append(line)
        return '\n'.join(lines).strip()

    def query(self, cmd: str, timeout: float = 3.0) -> str:
        """Sendet SCPI-Befehl und gibt Antwort zurueck (Thread-sicher via Queue)."""
        self.sock.sendall((cmd + '\r\n').encode())
        return self._collect_scpi_response(cmd, timeout)

    # ------------------------------------------------------------------
    # TOD-Datenstrom
    # ------------------------------------------------------------------

    def tod_enable(self):
        """Aktiviert den TOD-Datenstrom."""
        self.sock.sendall(b'TOD EN\r\n')
        # Bestaetigung abwarten
        self._collect_scpi_response('TOD EN', timeout=2.0)
        self._tod_enabled = True
        logging.info("TOD-Datenstrom aktiviert (1PPS-Pakete kommen jetzt)")

    def tod_disable(self):
        self.sock.sendall(b'TOD DI\r\n')
        self._collect_scpi_response('TOD DI', timeout=2.0)
        self._tod_enabled = False

    def recv_tod_packet(self, timeout: float = 2.0) -> Optional[Tuple[bytes, datetime]]:
        """
        Wartet auf das naechste TOD-Paket aus der Queue.
        Gibt (raw_bytes, recv_time) zurueck oder None bei Timeout/Fehler.
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
# SCPI-Antwort-Parser
# ---------------------------------------------------------------------------

def parse_time_string(resp: str) -> Optional[datetime]:
    """Parst "YYYY/MM/DD HH:MM:SS" -> datetime (UTC)."""
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
    Parst "N,+49,+18,+51.668,E,+6,+56,+21.080,+315.12"
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


def parse_tod_seconds_bcd(pkt: bytes) -> Optional[int]:
    """Extrahiert Sekunden (BCD) aus Byte 30 des TOD-Pakets."""
    if len(pkt) < 31 or pkt[0] != 0xC5:
        return None
    bcd = pkt[30]
    tens = (bcd >> 4) & 0x0F
    ones = bcd & 0x0F
    if tens > 5 or ones > 9:
        return None
    return tens * 10 + ones


# ---------------------------------------------------------------------------
# NMEA-Generator
# ---------------------------------------------------------------------------

class NmeaGenerator:
    def __init__(self):
        self.lat = 0.0
        self.lon = 0.0
        self.alt = 0.0
        self.num_sats = 0
        self.locked   = False
        self._lock    = threading.Lock()

    def update_position(self, lat: float, lon: float, alt: float):
        with self._lock:
            self.lat, self.lon, self.alt = lat, lon, alt

    def update_status(self, num_sats: int, locked: bool):
        with self._lock:
            self.num_sats = num_sats
            self.locked   = locked

    def generate(self, now: datetime) -> list:
        """Erzeugt NMEA-Saetze fuer den gegebenen UTC-Zeitpunkt."""
        with self._lock:
            lat, lon, alt = self.lat, self.lon, self.alt
            sats   = self.num_sats
            locked = self.locked

        status   = 'A' if locked else 'V'
        fix_qual = '1' if locked else '0'

        tstr = f"{now.hour:02d}{now.minute:02d}{now.second:02d}.00"
        dstr = f"{now.day:02d}{now.month:02d}{now.year % 100:02d}"

        lat_s, lat_h = deg_to_nmea_lat(lat)
        lon_s, lon_h = deg_to_nmea_lon(lon)

        sentences = []

        # $GPRMC
        sentences.append(build_nmea(
            'GPRMC', tstr, status,
            lat_s, lat_h, lon_s, lon_h,
            '0.000', '0.00', dstr, '', '', 'A'
        ))

        # $GPGGA
        sentences.append(build_nmea(
            'GPGGA', tstr,
            lat_s, lat_h, lon_s, lon_h,
            fix_qual, f"{sats:02d}",
            '1.0', f"{alt:.2f}", 'M', '47.0', 'M', '', ''
        ))

        # $GPZDA  (praziseste Zeitquelle fuer NTP)
        sentences.append(build_nmea(
            'GPZDA', tstr,
            f"{now.day:02d}", f"{now.month:02d}", f"{now.year:04d}",
            '00', '00'
        ))

        return sentences


# ---------------------------------------------------------------------------
# Haupt-Bridge
# ---------------------------------------------------------------------------

class UccmScpiBridge:
    """
    Verbindet UCCM SCPI -> gpsd-PTY + NTP-SHM.

    Architektur (Single-Thread-Polling + optionaler TOD-Thread):
      Haupt-Thread: SCPI-Polling (TIME:STRing?, GPS:POSition?, ...) @ 1 Hz
      TOD-Thread:   TOD-Paket-Empfang fuer praezises 1PPS-Timing (optional)
    """

    def __init__(self, args):
        self.host            = args.host
        self.port            = args.port
        self.pty_path        = args.pty
        self.reconnect_delay = args.reconnect_delay
        self.use_shm         = args.ntp_shm
        self.no_tod          = args.no_tod
        self.running         = False
        self._pty: Optional[GpsdPty]       = None
        self._shm0: Optional[NtpShm]       = None
        self._shm1: Optional[NtpShm]       = None
        self._nmea                         = NmeaGenerator()
        self._last_pos_time                = 0.0
        self._tod_client: Optional[UccmScpiClient] = None

    # --- Lifecycle ---

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
                    logging.warning(f"NTP SHM Unit {unit} nicht verfuegbar: {e}")

        self._thread = threading.Thread(target=self._main_loop,
                                        name='scpi', daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._pty:
            self._pty.close()
        for shm in (self._shm0, self._shm1):
            if shm:
                shm.close()

    def wait(self):
        self._thread.join()

    # --- Haupt-Schleife ---

    def _main_loop(self):
        while self.running:
            client = self._connect_scpi()
            if client is None:
                break
            try:
                self._run_session(client)
            except Exception as e:
                logging.error(f"Session-Fehler: {e}")
            finally:
                client.close()
            if self.running:
                logging.info(f"Verbindung unterbrochen. Reconnect in "
                             f"{self.reconnect_delay}s...")
                time.sleep(self.reconnect_delay)

    def _connect_scpi(self) -> Optional[UccmScpiClient]:
        while self.running:
            try:
                logging.info(f"Verbinde SCPI zu {self.host}:{self.port} ...")
                c = UccmScpiClient(self.host, self.port)
                c.connect()
                return c
            except OSError as e:
                logging.warning(f"Verbindung fehlgeschlagen: {e}. "
                                f"Retry in {self.reconnect_delay}s ...")
                time.sleep(self.reconnect_delay)
        return None

    # --- Session ---

    def _run_session(self, client: UccmScpiClient):
        """Fuehrt eine SCPI-Session durch: initiales Setup + Polling-Loop."""

        # Geraet identifizieren
        idn = client.query('*IDN?', timeout=3)
        logging.info(f"UCCM IDN: {idn.strip()!r}")

        # Initiale Position holen
        self._update_position(client)
        self._update_status(client)

        # Optionaler TOD-Thread fuer 1PPS-Timing
        tod_thread = None
        if not self.no_tod and self._shm1:
            client.tod_enable()
            self._tod_client = client
            tod_thread = threading.Thread(
                target=self._tod_loop, args=(client,),
                name='tod', daemon=True
            )
            tod_thread.start()

        # Polling-Loop: 1x pro Sekunde TIME:STRing? + periodisch GPS-Daten
        poll_cycle  = 0
        last_second = -1

        while self.running:
            t_start = time.time()

            # Uhrzeit abfragen
            try:
                resp = client.query('TIME:STRing?', timeout=3)
            except Exception as e:
                raise ConnectionError(f"TIME:STRing? fehlgeschlagen: {e}")

            recv_time = datetime.now(timezone.utc)
            gps_time  = parse_time_string(resp)

            if gps_time is None:
                logging.warning(f"Ungueltige TIME:STRing-Antwort: {resp!r}")
                time.sleep(0.5)
                continue

            # Nur einmal pro Sekunde NMEA erzeugen
            if gps_time.second != last_second:
                last_second = gps_time.second
                logging.debug(f"GPS-Zeit: {gps_time.isoformat()} "
                              f"(Empfang: {recv_time.isoformat()})")

                # NMEA erzeugen und in PTY schreiben
                sentences = self._nmea.generate(gps_time)
                for s in sentences:
                    self._pty.write(s)
                    logging.debug(f"NMEA: {s.decode().strip()}")

                # NTP SHM0 (NMEA-Timing)
                if self._shm0:
                    self._shm0.write(gps_time, recv_time, precision=-1)
                    logging.debug(f"NTP SHM0: {gps_time.isoformat()}")

            # Periodische Abfragen (jede 30. Sekunde)
            poll_cycle += 1
            if poll_cycle % 30 == 0 or poll_cycle == 1:
                try:
                    self._update_position(client)
                    self._update_status(client)
                except Exception as e:
                    logging.warning(f"Periodische Abfrage fehlgeschlagen: {e}")

            # Genau 1 Sekunde Abstand halten
            elapsed = time.time() - t_start
            sleep_s = max(0, 1.0 - elapsed)
            time.sleep(sleep_s)

        if tod_thread:
            tod_thread.join(timeout=2)

    def _tod_loop(self, client: UccmScpiClient):
        """
        Empfaengt TOD-Pakete in einem separaten Thread.
        Nutzt den Ankunftszeitpunkt des Pakets als 1PPS-Referenz.
        """
        logging.info("TOD-Thread gestartet")
        while self.running and self._tod_client is client:
            result = client.recv_tod_packet(timeout=2.0)
            if result is None:
                continue
            pkt, recv_time = result
            sec_bcd = parse_tod_seconds_bcd(pkt)
            logging.debug(f"TOD-Paket empfangen (Sekunde BCD={sec_bcd:#04x}), "
                          f"Empfang: {recv_time.isoformat()}")

            # GPS-Zeit fuer SHM1: naechste volle Sekunde (das ist die angekuendigte)
            # Wir verwenden die Sekunde des Empfangszeitpunkts (gerundet)
            gps_sec = recv_time.replace(microsecond=0)
            if self._shm1:
                self._shm1.write(gps_sec, recv_time, precision=-9)
                logging.debug(f"NTP SHM1 (1PPS): {gps_sec.isoformat()}")

        logging.info("TOD-Thread beendet")

    # --- Hilfsmethoden ---

    def _update_position(self, client: UccmScpiClient):
        resp = client.query('GPS:POSition?', timeout=3)
        pos  = parse_position(resp)
        if pos:
            lat, lon, alt = pos
            self._nmea.update_position(lat, lon, alt)
            logging.info(f"Position: {lat:+.6f} {lon:+.6f} Alt={alt:.1f}m")
        else:
            logging.warning(f"Position nicht parsebar: {resp!r}")

    def _update_status(self, client: UccmScpiClient):
        try:
            sats_resp = client.query('GPS:SATellite:TRACking:COUNt?', timeout=3)
            lock_resp = client.query('LED:GPSLock?', timeout=3)
            tfom_resp = client.query('SYNChronization:TFOMerit?', timeout=3)
            sats   = parse_sat_count(sats_resp)
            locked = parse_gps_lock(lock_resp)
            self._nmea.update_status(sats, locked)
            logging.info(f"Status: {sats} Satelliten, Lock={locked}, TFOM={tfom_resp.split(chr(10))[0].strip()!r}")
        except Exception as e:
            logging.warning(f"Status-Abfrage fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# Signal-Handler & Argument-Parser
# ---------------------------------------------------------------------------

def make_signal_handler(bridge: UccmScpiBridge):
    def handler(sig, frame):
        logging.info(f"Signal {sig} empfangen, beende ...")
        bridge.stop()
    return handler


def parse_args():
    parser = argparse.ArgumentParser(
        description='Samsung UCCM SCPI-Bridge: TCP SCPI -> PTY (gpsd) + NTP SHM',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('host',
                        help='Hostname/IP des UCCM (z.B. 172.16.20.30)')
    parser.add_argument('port', type=int,
                        help='TCP-Port der UCCM-CLI (typisch: 2000)')
    parser.add_argument('--pty', default='/dev/uccm_gps',
                        help='PTY-Symlink-Pfad fuer gpsd')
    parser.add_argument('--reconnect-delay', type=float, default=5.0,
                        metavar='SEK')
    parser.add_argument('--ntp-shm', action='store_true',
                        help='NTP SHM aktivieren (Unit 0=NMEA, Unit 1=TOD-1PPS)')
    parser.add_argument('--no-tod', action='store_true',
                        help='TOD-Datenstrom nicht aktivieren '
                             '(kein praezises 1PPS-SHM, aber kein Mux-Problem)')
    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return parser.parse_args()


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
    logging.info(f"Bridge laeuft. PTY: {args.pty}")
    if args.ntp_shm:
        logging.info("NTP SHM aktiv:")
        logging.info("  server 127.127.28.0 minpoll 4 maxpoll 4    # NMEA")
        logging.info("  fudge  127.127.28.0 refid GPS")
        logging.info("  server 127.127.28.1 minpoll 4 maxpoll 4 prefer  # TOD-1PPS")
        logging.info("  fudge  127.127.28.1 refid PPS")

    try:
        bridge.wait()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    logging.info("Bridge beendet.")


if __name__ == '__main__':
    main()

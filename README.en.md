# Samsung UCCM GPS Bridge

Connects a Samsung UCCM GPS module via TCP (SCPI-CLI, port 2000) or directly
via a serial port to `gpsd` using a PTY, and optionally to `ntpd`/`chrony`
via NTP SHM (reference clock type 28).

## Architecture

### TCP mode (default)

```
Samsung UCCM GPS
        | TCP port 2000 (SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI polling (TIME:STRing?, GPS:POSition?, ...)  ── 1 Hz
  ├── TOD data stream (1PPS packets via TOD EN)        ── 1x/s
  ├── Log queries (separate TCP connection)            ── on demand
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### Serial mode (--serial)

```
Samsung UCCM GPS
        | /dev/ttyUSB0 (serial, SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI polling (TIME:STRing?, GPS:POSition?, ...)  ── 1 Hz
  ├── DCD pin polling (1PPS via rising edge)           ── 1 ms interval
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### 1PPS sources (`--pps-source`)

| `--pps-source` | TCP mode           | Serial mode            | Typical SHM1 jitter |
|----------------|--------------------|------------------------|---------------------|
| `auto`         | TOD packets        | DCD pin                | TCP: 100–700 ms / DCD: < 2 ms (¹) |
| `tod`          | TOD packets        | TOD packets            | TCP: 100–700 ms / serial: ~5 ms |
| `dcd`          | — (warning)        | DCD pin                | < 2 ms (¹)          |
| `none`         | no 1PPS            | no 1PPS                | inactive            |

(¹) Only with FTDI latency timer = 1 ms (see section below). The default value
of 16 ms adds up to 16 ms of additional jitter to DCD-based PPS measurements.

**Recommendation:** For PPS quality (< 5 ms jitter) use serial mode with `auto`
(= DCD pin, 1 ms polling) and FTDI low-latency mode. TOD over TCP is subject to
variable network and OS scheduling jitter (100–700 ms) and is not recommended as
a primary PPS source for NTP — when using TOD over TCP, compensate the NTP
offset with `fudge time2` (ntpd) or `offset` (chrony) (typical value: read the
SHM1 offset from `ntpq -p` / `chronyc sources` and enter it with the opposite sign).

`--no-tod` is an alias for `--pps-source none` (backwards compatibility).

**Note:** The bridge writes 1PPS directly to NTP SHM Unit 1 (bypassing gpsd).
`gpsmon` therefore shows PPS as N/A — ntpd/chrony still receive the data correctly.

## Requirements

- Python >= 3.8 (standard library, no external dependencies)
- `gpsd` >= 3.x
- `ntpd` or `chrony` (for NTP operation)
- Linux (PTY, SYSV SHM, TIOCMGET for DCD)
- Network access to the UCCM on TCP port 2000 (TCP mode)
  or a serial port with SCPI-CLI (serial mode)

## ser2net (optional serial network access)

If the UCCM is physically connected via a serial port but the bridge runs on a
different machine, `ser2net` can expose the port over TCP.
The included `ser2net.yaml` configures two ports:

| TCP port | Protocol  | Purpose                              |
|----------|-----------|--------------------------------------|
| 2001     | RFC 2217  | DCD/1PPS transfer as modem state     |
| 2002     | Raw TCP   | Raw NMEA byte stream for gpsd / nc   |

```bash
# Adjust device path in ser2net.yaml (/dev/ttyUSB0 or similar)
sudo cp ser2net.yaml /etc/ser2net.yaml
sudo systemctl enable --now ser2net
```

If the serial port is directly connected to the bridge machine, ser2net is not
needed — `--serial /dev/ttyUSB0` is sufficient.

## FTDI USB-serial: low-latency mode (serial mode)

When using an FTDI-based USB-serial adapter (e.g. `/dev/ttyUSB0`), the FTDI
chip buffers incoming data for up to **16 ms** by default (latency timer) before
transferring it to the host over USB. This also affects modem status events such
as DCD edges and directly causes **up to 16 ms of jitter** on SHM Unit 1 when
using `--pps-source dcd`.

Set the latency timer to 1 ms (as root, not persistent):

```bash
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
# Verify:
cat /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

Persistent via udev rule (`/etc/udev/rules.d/99-ftdi-latency.rules`):

```
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
```

Activate the rule:

```bash
sudo udevadm control --reload-rules
# Re-plug the adapter or:
sudo udevadm trigger --subsystem-match=usb-serial
```

**Note:** The FTDI latency timer is only relevant for serial mode (`--serial`)
with DCD-based 1PPS. It has no effect in TCP mode (TOD packets).

## Installation

```bash
# Install script
sudo cp uccm_scpi_bridge.py /usr/local/bin/uccm_scpi_bridge.py
sudo chmod +x /usr/local/bin/uccm_scpi_bridge.py

# Install systemd service
sudo cp uccm-scpi-bridge.service /etc/systemd/system/
# Adjust HOST in the service file!
sudo nano /etc/systemd/system/uccm-scpi-bridge.service

sudo systemctl daemon-reload
sudo systemctl enable uccm-scpi-bridge
sudo systemctl start uccm-scpi-bridge
```

## Usage

### Run directly

```bash
# TCP mode (minimal, NMEA -> gpsd only)
./uccm_scpi_bridge.py 192.168.1.100 2000

# TCP mode with NTP SHM (as root or with CAP_IPC_OWNER)
sudo ./uccm_scpi_bridge.py --ntp-shm 192.168.1.100 2000

# TCP mode without 1PPS
sudo ./uccm_scpi_bridge.py --ntp-shm --pps-source none 192.168.1.100 2000

# Serial mode, 1PPS via DCD pin (default, lowest jitter)
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --baud 9600

# Serial mode, 1PPS via TOD data stream instead of DCD
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --pps-source tod

# With web interface
sudo ./uccm_scpi_bridge.py --ntp-shm --web-port 8080 192.168.1.100 2000

# All options
./uccm_scpi_bridge.py --help
```

### Options

| Option              | Default         | Description                                            |
|---------------------|-----------------|--------------------------------------------------------|
| `host`              | —               | Hostname/IP of the UCCM (TCP mode)                     |
| `port`              | —               | TCP port of the UCCM CLI (typically 2000)              |
| `--serial`          | —               | Serial device instead of TCP (e.g. `/dev/ttyUSB0`)     |
| `--baud`            | `9600`          | Baud rate for serial device                            |
| `--pty`             | `/dev/uccm_gps` | PTY symlink path for gpsd                              |
| `--ntp-shm`         | off             | Enable NTP SHM (Unit 0=NMEA, Unit 1=1PPS)              |
| `--pps-source`      | `auto`          | 1PPS source: `auto`\|`tod`\|`dcd`\|`none`             |
| `--no-tod`          | off             | Alias for `--pps-source none`                          |
| `--reconnect-delay` | `5.0`           | Seconds until reconnect                                |
| `--web-port`        | `0`             | HTTP status port (0 = disabled, e.g. `8080`)           |
| `--log-level`       | `INFO`          | DEBUG/INFO/WARNING/ERROR                               |

`host`/`port` and `--serial` are mutually exclusive.

## Configuring gpsd

```ini
# /etc/default/gpsd
DEVICES="/dev/uccm_gps"
GPSD_OPTIONS="-n"
START_DAEMON="true"
```

Or manually:
```bash
gpsd -n -N /dev/uccm_gps
```

## Configuring NTP

The bridge must be started with `--ntp-shm` as root. On first start, remove
any stale SHM segments:

```bash
ipcrm -M 0x4e545030 -M 0x4e545031 2>/dev/null
sudo systemctl restart uccm-scpi-bridge
```

### NTP SHM timing

| SHM Unit | Content | clockTime | receiveTime | Offset |
|----------|---------|-----------|-------------|--------|
| 0 (GPS)  | GPS second (TIME:STRing?) | GPS whole second | System time after SCPI response | ≈ −(SCPI latency, ~200–400 ms) |
| 1 (PPS)  | TOD packet or DCD edge | GPS second (BCD) | System time at reception | depends on source (see below) |

**SHM0 (GPS NMEA):** The offset is negative and equals the SCPI query latency
(typically 200–400 ms). NTP learns this value automatically. Optionally it can
be compensated with `fudge time1` — read the exact value from `ntpq -p`
(column `offset`) and enter it with the opposite sign.

**SHM1 (1PPS):** Jitter and systematic offset depend on the PPS source:

| PPS source                    | Jitter     | Systematic offset | Recommendation |
|-------------------------------|------------|-------------------|----------------|
| DCD serial (FTDI latency=1)   | < 2 ms     | < 1 ms            | Recommended as primary PPS source |
| DCD serial (FTDI latency=16)  | up to 16 ms | < 8 ms           | Acceptable, but adjust FTDI latency |
| TOD serial                    | ~5 ms      | < 5 ms            | Acceptable; UART transmission is deterministic |
| TOD TCP                       | 100–700 ms | 100–500 ms        | Not recommended as primary PPS source; set `fudge time2` / `offset` |

For TOD TCP: read the measured SHM1 offset from `ntpq -p` or `chronyc sources`
(column `offset`) and enter it with the **opposite sign** as `fudge time2` (ntpd)
or `offset` (chrony). This compensates the fixed delay component.

**Determining the offset value (TOD TCP):**

```bash
# ntpd: read offset column (in ms), flip sign, convert to seconds
ntpq -p
# Example: SHM(1) shows offset = -195.000 ms  →  time2 = +0.195

# chrony: read Offset column
chronyc sources -v
# Example: SHM(1) shows -195 ms  →  offset = +0.195
```

This value is typically stable (< 20 ms variation) and only needs to be measured
once. After restarting ntpd/chrony with the corrected value, SHM1 should show an
offset < 20 ms and be accepted as a valid source.

### ntpd (`/etc/ntp.conf`)

```
# GPS NMEA (SHM Unit 0) - coarse accuracy ~0.5 s, offset from SCPI latency
server 127.127.28.0 minpoll 4 maxpoll 4
fudge  127.127.28.0 refid GPS time1 0.3
# time1: set to the measured offset from "ntpq -p" with the opposite sign

# 1PPS via TOD/DCD (SHM Unit 1) - fine accuracy (serial DCD/TOD recommended)
server 127.127.28.1 minpoll 4 maxpoll 4 prefer
fudge  127.127.28.1 refid PPS
# time2: only needed for TOD TCP (systematic delay offset)
# Value = measured SHM1 offset from "ntpq -p" with opposite sign, e.g.:
# fudge  127.127.28.1 refid PPS time2 0.2

# Recommendation: add a fallback time server to prevent clock drift
# if GPS sources are marked as falsetickers:
# server pool.ntp.org iburst
```

### chrony (`/etc/chrony.conf`)

```
# GPS NMEA (SHM 0) - offset: adjust from measured value in "chronyc sources"
refclock SHM 0 refid GPS precision 1e-1 offset 0.3 poll 4
# offset: positive value = SCPI latency in seconds (typically 0.2–0.4)

# 1PPS via TOD/DCD (SHM 1) - serial DCD or TOD recommended
refclock SHM 1 refid PPS precision 1e-9 prefer poll 4
# offset: only needed for TOD TCP (systematic delay offset)
# Value = measured SHM1 offset from "chronyc sources" with opposite sign, e.g.:
# refclock SHM 1 refid PPS precision 1e-9 prefer poll 4 offset 0.2

# Recommendation: add a fallback time server to prevent clock drift
# if GPS sources are marked as falsetickers:
# pool pool.ntp.org iburst
```

## Web Interface

With `--web-port 8080` the bridge starts a built-in HTTP server:

```bash
sudo ./uccm_scpi_bridge.py --ntp-shm --web-port 8080 192.168.1.100 2000
```

Open in browser: `http://<bridge-host>:8080/`

The page refreshes automatically every 2 seconds and shows:

| Field          | Description                                      |
|----------------|--------------------------------------------------|
| Connection     | TCP/serial connected or disconnected             |
| GPS Lock       | Locked / Unlocked                                |
| GPS Time       | Last UTC timestamp from the device               |
| Position       | Latitude / Longitude / Altitude                  |
| Satellites     | Count + PRN list                                 |
| TFOM           | Time accuracy (SYNChronization:TFOMerit?)        |
| ANT Voltage    | Antenna supply voltage                           |
| ANT Current    | Antenna current draw                             |
| Temperature    | Device temperature                               |
| 1PPS Source    | Configured PPS source (auto/tod/dcd/none)        |
| Last PPS       | Timestamp of the last 1PPS pulse                 |
| Bridge Start   | Bridge start time                                |

ANT voltage, ANT current, and temperature are read every 30 seconds from
`SYSTEM:STATUS?`.

### Diagnostic Log

The web interface allows fetching and clearing the internal UCCM diagnostic log:

- **Load Log** — reads the log via `DIAGNOSTIC:LOG:READ:ALL?`
- **Clear Log** — clears the log via `DIAGNOSTIC:LOG:CLEAR`

In TCP mode, log operations use a **separate TCP connection** so the SCPI
polling loop (and therefore NTP SHM timestamps) is not interrupted.

### HTTP API

| Endpoint         | Method | Description                               |
|------------------|--------|-------------------------------------------|
| `/`              | GET    | Web interface (HTML)                      |
| `/status`        | GET    | Bridge status as JSON                     |
| `/log`           | GET    | Diagnostic log as JSON (`{"lines": [...]}`) |
| `/log/clear`     | POST   | Clear diagnostic log                      |

## Diagnostics

```bash
# Bridge status
sudo systemctl status uccm-scpi-bridge
journalctl -fu uccm-scpi-bridge

# Check gpsd
gpspipe -r /dev/uccm_gps       # Raw NMEA
cgps -s                         # Interactive display
gpsmon                          # Detail monitor (shows PRN list via GPGSA)

# Check NTP SHM (active segments with nattch > 0 = ntpd connected)
ipcs -m | grep "4e5450"
ntpq -p
chronyc sources -v
```

**Note on gpsd LOG_PROG messages:** Messages like
`GPRMC starts a reporting cycle` or `xxGSA sets mode 3` are normal
debug output (log level PROG), not errors.

**Note on PPS in gpsmon:** gpsmon shows PPS as N/A because the bridge
writes the 1PPS timestamp directly to NTP SHM and gpsd does not see a separate
PPS device. ntpd/chrony still receive the PPS data correctly via SHM Unit 1.

## Samsung UCCM — Protocol Details

### NMEA sentences (generated by the bridge)

The bridge generates the following NMEA-0183 sentences from SCPI data, in this
order, once per second:

| Sentence  | Content                                | Note                            |
|-----------|----------------------------------------|---------------------------------|
| `$GPRMC`  | Time, position, status                 | Cycle starter (gpsd)            |
| `$GPZDA`  | UTC date and time                      | Precise time source for NTP     |
| `$GPGSA`  | Fix type, active satellites (PRNs)     | gpsmon shows satellite list     |
| `$GPGGA`  | Fix quality, satellite count, altitude | Cycle ender (gpsd)              |

`$GPGSV` is not generated: the UCCM SCPI interface provides no
elevation/azimuth/SNR data per satellite. gpsd 3.25 would reject sentences with
all azimuths = 0 as invalid (SiRFstar workaround).

### SCPI commands

| Command | Response format | Polling interval |
|---|---|---|
| `TIME:STRing?` | `YYYY/MM/DD HH:MM:SS` | 1 Hz |
| `GPS:POSition?` | `N,+DD,+MM,+SS.sss,E,+DD,+MM,+SS.sss,+HHH.hh` | every 30 s |
| `GPS:SATellite:TRACking:COUNt?` | `+N` | every 30 s |
| `GPS:SATellite:TRACking?` | `+prn,...,+prn` | every 30 s |
| `LED:GPSLock?` | `Locked` / `Unlocked` | every 30 s |
| `SYNChronization:TFOMerit?` | e.g. `1 ~ 10 nsec` | every 30 s |
| `SYSTEM:STATUS?` | Multi-line status block (ANT, temp, ...) | every 30 s |
| `DIAGNOSTIC:LOG:READ:ALL?` | Log entries line by line | on demand (web UI) |
| `DIAGNOSTIC:LOG:CLEAR` | — | on demand (web UI) |
| `TOD EN` / `TOD DI` | — | once at startup (`--pps-source tod`) |

### TOD packets (1PPS via `--pps-source tod`)

44 bytes binary, hex-encoded in ASCII, one packet per second.
Sync byte: `0xC5`. Byte 30 contains the second in BCD format (0x00–0x59).

The bridge uses the BCD seconds field for plausibility checking and corrects
`gps_sec` by ±1 s if the packet arrived near a second boundary and
`recv_time` would otherwise round to the wrong second.

## License

MIT

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
  ├── SCPI polling (TIME:STRing?, GPS:POSition?, ...)
  ├── TOD data stream (1PPS packets via TOD EN, 1x/s)
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### Serial mode (--serial)

```
Samsung UCCM GPS
        | /dev/ttyUSB0 (serial, SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI polling (TIME:STRing?, GPS:POSition?, ...)
  ├── DCD pin polling (1PPS via rising edge)
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### 1PPS sources (`--pps-source`)

| `--pps-source` | TCP mode           | Serial mode            | SHM Unit 1 |
|----------------|--------------------|------------------------|------------|
| `auto`         | TOD packets        | DCD pin                | ~1 µs      |
| `tod`          | TOD packets        | TOD packets            | ~1 µs      |
| `dcd`          | — (warning)        | DCD pin                | ~1 µs      |
| `none`         | no 1PPS            | no 1PPS                | inactive   |

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

# Serial mode, 1PPS via DCD pin (default)
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --baud 9600

# Serial mode, 1PPS via TOD data stream instead of DCD
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --pps-source tod

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

### ntpd (`/etc/ntp.conf`)

```
# GPS NMEA (SHM Unit 0) - ~100 ms accuracy
server 127.127.28.0 minpoll 4 maxpoll 4
fudge  127.127.28.0 refid GPS time1 0.0

# 1PPS via TOD/DCD (SHM Unit 1) - ~1 µs accuracy
server 127.127.28.1 minpoll 4 maxpoll 4 prefer
fudge  127.127.28.1 refid PPS
```

### chrony (`/etc/chrony.conf`)

```
# GPS NMEA (SHM 0)
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 poll 4

# 1PPS via TOD/DCD (SHM 1)
refclock SHM 1 refid PPS precision 1e-9 prefer poll 4
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
| `TOD EN` / `TOD DI` | - | once at startup (`--pps-source tod`) |

### TOD packets (1PPS via `--pps-source tod`)

44 bytes binary, hex-encoded in ASCII, one packet per second.
Sync byte: `0xC5`. Byte 30 contains the second in BCD format.

## License

MIT

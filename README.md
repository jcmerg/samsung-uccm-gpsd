# Samsung UCCM GPS Bridge

Verbindet ein Samsung UCCM GPS-Modul direkt per TCP (SCPI-CLI, Port 2000) mit
`gpsd` via PTY und optional mit `ntpd`/`chrony` via NTP SHM (Referenzuhr Typ 28).

## Architektur

```
Samsung UCCM GPS
        | TCP Port 2000 (SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI-Polling (TIME:STRing?, GPS:POSition?, ...)
  ├── TOD-Datenstrom (1PPS-Pakete, 1x/s)
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS-Clients (DBUS, socket)
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony (NTP-Server)
```

### 1PPS

Das 1PPS-Signal wird ueber den TOD-Datenstrom des UCCM bezogen (`TOD EN`).
Das UCCM sendet einmal pro Sekunde ein 44-Byte-Binaerpaket (hex-kodiert in ASCII).
Der Ankunftszeitpunkt des Pakets wird als SHM Unit 1 eingetragen.

## Voraussetzungen

- Python >= 3.8 (Standardbibliothek, keine externen Abhaengigkeiten)
- `gpsd` >= 3.x
- `ntpd` oder `chrony` (fuer NTP-Betrieb)
- Linux (PTY und SYSV SHM)
- Netzwerkzugang zum UCCM auf TCP Port 2000

## Installation

```bash
# Script installieren
sudo cp uccm_scpi_bridge.py /usr/local/bin/uccm_scpi_bridge.py
sudo chmod +x /usr/local/bin/uccm_scpi_bridge.py

# Systemd-Service installieren
sudo cp uccm-scpi-bridge.service /etc/systemd/system/
# HOST in der Service-Datei anpassen!
sudo nano /etc/systemd/system/uccm-scpi-bridge.service

sudo systemctl daemon-reload
sudo systemctl enable uccm-scpi-bridge
sudo systemctl start uccm-scpi-bridge
```

## Verwendung

### Direkt starten

```bash
# Minimal (nur NMEA -> gpsd)
./uccm_scpi_bridge.py 192.168.1.100 2000

# Mit NTP SHM (als root oder mit CAP_IPC_OWNER)
sudo ./uccm_scpi_bridge.py --ntp-shm 192.168.1.100 2000

# Ohne TOD-Datenstrom (kein praezises 1PPS-SHM)
sudo ./uccm_scpi_bridge.py --ntp-shm --no-tod 192.168.1.100 2000

# Alle Optionen
./uccm_scpi_bridge.py --help
```

### Optionen

| Option              | Standard        | Beschreibung                                       |
|---------------------|-----------------|----------------------------------------------------|
| `host`              | (Pflicht)       | Hostname/IP des UCCM                               |
| `port`              | (Pflicht)       | TCP-Port der UCCM-CLI (typisch: 2000)              |
| `--pty`             | `/dev/uccm_gps` | PTY-Symlink fuer gpsd                              |
| `--ntp-shm`         | aus             | NTP SHM aktivieren                                 |
| `--no-tod`          | aus             | TOD-Datenstrom nicht aktivieren (nur NMEA-Timing)  |
| `--reconnect-delay` | `5.0`           | Sekunden bis Reconnect                             |
| `--log-level`       | `INFO`          | DEBUG/INFO/WARNING/ERROR                           |

## gpsd konfigurieren

```ini
# /etc/default/gpsd
DEVICES="/dev/uccm_gps"
GPSD_OPTIONS="-n"
START_DAEMON="true"
```

Oder manuell:
```bash
gpsd -n -N /dev/uccm_gps
```

## NTP konfigurieren

### ntpd (`/etc/ntp.conf`)

```
# GPS NMEA (SHM Unit 0) - ~100ms Genauigkeit
server 127.127.28.0 minpoll 4 maxpoll 4
fudge  127.127.28.0 refid GPS time1 0.0

# 1PPS via TOD (SHM Unit 1) - ~1µs Genauigkeit
server 127.127.28.1 minpoll 4 maxpoll 4 prefer
fudge  127.127.28.1 refid PPS
```

### chrony (`/etc/chrony.conf`)

```
# GPS NMEA (SHM 0)
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 poll 4

# 1PPS via TOD (SHM 1)
refclock SHM 1 refid PPS precision 1e-9 prefer poll 4
```

## Diagnose

```bash
# Bridge-Status
sudo systemctl status uccm-scpi-bridge
journalctl -fu uccm-scpi-bridge

# gpsd pruefen
gpspipe -r /dev/uccm_gps       # Rohe NMEA
cgps -s                         # Interaktive Anzeige
gpsmon                          # Detailmonitor

# NTP SHM pruefen
ntpq -p
chronyc sources -v
```

## Samsung UCCM - Protokoll-Details

### NMEA-Saetze (generiert durch Bridge)

Die Bridge erzeugt aus den SCPI-Daten folgende NMEA-0183-Saetze:
- `$GPRMC` - Zeit, Position, Geschwindigkeit
- `$GPGGA` - Fix-Informationen
- `$GPZDA` - UTC-Zeit und Datum (praeziseste Zeitquelle fuer NTP)

### SCPI-Befehle

| Befehl | Antwort-Format | Polling-Intervall |
|---|---|---|
| `TIME:STRing?` | `YYYY/MM/DD HH:MM:SS` | 1 Hz |
| `GPS:POSition?` | `N,+DD,+MM,+SS.sss,E,+DD,+MM,+SS.sss,+HHH.hh` | alle 30 s |
| `GPS:SATellite:TRACking:COUNt?` | `+N` | alle 30 s |
| `LED:GPSLock?` | `Locked` / `Unlocked` | alle 30 s |
| `SYNChronization:TFOMerit?` | z.B. `1 ~ 10 nsec` | alle 30 s |
| `TOD EN` / `TOD DI` | - | einmalig beim Start |

### TOD-Pakete (1PPS)

44 Bytes binaer, hex-kodiert in ASCII, ein Paket pro Sekunde.
Sync-Byte: `0xC5`. Byte 30 enthaelt die Sekunde im BCD-Format.

## Lizenz

MIT

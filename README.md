# Samsung UCCM GPS Bridge

Verbindet ein Samsung UCCM GPS-Modul per TCP (SCPI-CLI, Port 2000) oder direkt
ueber einen seriellen Port mit `gpsd` via PTY und optional mit `ntpd`/`chrony`
via NTP SHM (Referenzuhr Typ 28).

## Architektur

### TCP-Modus (Standard)

```
Samsung UCCM GPS
        | TCP Port 2000 (SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI-Polling (TIME:STRing?, GPS:POSition?, ...)
  ├── TOD-Datenstrom (1PPS-Pakete via TOD EN, 1x/s)
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS-Clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### Serieller Modus (--serial)

```
Samsung UCCM GPS
        | /dev/ttyUSB0 (seriell, SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI-Polling (TIME:STRing?, GPS:POSition?, ...)
  ├── DCD-Pin-Polling (1PPS via steigende Flanke)
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS-Clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### 1PPS-Quellen (`--pps-source`)

| `--pps-source` | TCP-Modus          | Serieller Modus        | SHM Unit 1 |
|----------------|--------------------|------------------------|------------|
| `auto`         | TOD-Pakete         | DCD-Pin                | ~1 µs      |
| `tod`          | TOD-Pakete         | TOD-Pakete             | ~1 µs      |
| `dcd`          | — (Warnung)        | DCD-Pin                | ~1 µs      |
| `none`         | kein 1PPS          | kein 1PPS              | inaktiv    |

`--no-tod` ist ein Alias fuer `--pps-source none` (Rueckwaertskompatibilitaet).

**Hinweis:** Die Bridge schreibt 1PPS direkt in NTP SHM Unit 1 (bypassing gpsd).
`gpsmon` zeigt daher PPS als N/A — ntpd/chrony erhalten die Daten trotzdem korrekt.

## Voraussetzungen

- Python >= 3.8 (Standardbibliothek, keine externen Abhaengigkeiten)
- `gpsd` >= 3.x
- `ntpd` oder `chrony` (fuer NTP-Betrieb)
- Linux (PTY, SYSV SHM, TIOCMGET fuer DCD)
- Netzwerkzugang zum UCCM auf TCP Port 2000 (TCP-Modus)
  oder serieller Port mit SCPI-CLI (serieller Modus)

## ser2net (optionaler serieller Netzwerkzugang)

Ist der UCCM physisch per seriellem Port angebunden, aber die Bridge laeuft
auf einem anderen Rechner, kann `ser2net` den Port ueber TCP bereitstellen.
Die mitgelieferte `ser2net.yaml` konfiguriert zwei Ports:

| TCP-Port | Protokoll | Verwendung |
|----------|-----------|------------|
| 2001     | RFC 2217  | DCD/1PPS-Uebertragung als Modem-State |
| 2002     | Roh-TCP   | NMEA-Bytestream fuer gpsd / nc |

```bash
# Geraetepfad in ser2net.yaml anpassen (/dev/ttyUSB0 o.ae.)
sudo cp ser2net.yaml /etc/ser2net.yaml
sudo systemctl enable --now ser2net
```

Bei direkter Verbindung des seriellen Ports am Bridge-Rechner ist ser2net
nicht noetig — `--serial /dev/ttyUSB0` genuegt.

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
# TCP-Modus (minimal, nur NMEA -> gpsd)
./uccm_scpi_bridge.py 192.168.1.100 2000

# TCP-Modus mit NTP SHM (als root oder mit CAP_IPC_OWNER)
sudo ./uccm_scpi_bridge.py --ntp-shm 192.168.1.100 2000

# TCP-Modus ohne 1PPS
sudo ./uccm_scpi_bridge.py --ntp-shm --pps-source none 192.168.1.100 2000

# Serieller Modus, 1PPS via DCD-Pin (Standard)
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --baud 9600

# Serieller Modus, 1PPS via TOD-Datenstrom statt DCD
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --pps-source tod

# Alle Optionen
./uccm_scpi_bridge.py --help
```

### Optionen

| Option              | Standard        | Beschreibung                                           |
|---------------------|-----------------|--------------------------------------------------------|
| `host`              | —               | Hostname/IP des UCCM (TCP-Modus)                       |
| `port`              | —               | TCP-Port der UCCM-CLI (typisch: 2000)                  |
| `--serial`          | —               | Serielles Geraet statt TCP (z.B. `/dev/ttyUSB0`)       |
| `--baud`            | `9600`          | Baudrate fuer serielles Geraet                         |
| `--pty`             | `/dev/uccm_gps` | PTY-Symlink-Pfad fuer gpsd                             |
| `--ntp-shm`         | aus             | NTP SHM aktivieren (Unit 0=NMEA, Unit 1=1PPS)          |
| `--pps-source`      | `auto`          | 1PPS-Quelle: `auto`\|`tod`\|`dcd`\|`none`             |
| `--no-tod`          | aus             | Alias fuer `--pps-source none`                         |
| `--reconnect-delay` | `5.0`           | Sekunden bis Reconnect                                 |
| `--log-level`       | `INFO`          | DEBUG/INFO/WARNING/ERROR                               |

`host`/`port` und `--serial` schliessen sich gegenseitig aus.

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

Die Bridge muss mit `--ntp-shm` als root gestartet werden. Beim ersten Start
sollten vorhandene veraltete SHM-Segmente entfernt werden:

```bash
ipcrm -M 0x4e545030 -M 0x4e545031 2>/dev/null
sudo systemctl restart uccm-scpi-bridge
```

### ntpd (`/etc/ntp.conf`)

```
# GPS NMEA (SHM Unit 0) - ~100ms Genauigkeit
server 127.127.28.0 minpoll 4 maxpoll 4
fudge  127.127.28.0 refid GPS time1 0.0

# 1PPS via TOD/DCD (SHM Unit 1) - ~1µs Genauigkeit
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

## Diagnose

```bash
# Bridge-Status
sudo systemctl status uccm-scpi-bridge
journalctl -fu uccm-scpi-bridge

# gpsd pruefen
gpspipe -r /dev/uccm_gps       # Rohe NMEA
cgps -s                         # Interaktive Anzeige
gpsmon                          # Detailmonitor (zeigt PRN-Liste via GPGSA)

# NTP SHM pruefen (aktive Segmente mit nattch > 0 = ntpd verbunden)
ipcs -m | grep "4e5450"
ntpq -p
chronyc sources -v
```

**Hinweis zu gpsd LOG_PROG-Meldungen:** Meldungen wie
`GPRMC starts a reporting cycle` oder `xxGSA sets mode 3` sind normale
Debug-Ausgaben (Loglevel PROG), keine Fehler.

**Hinweis zu PPS in gpsmon:** gpsmon zeigt PPS als N/A, da die Bridge
den 1PPS-Zeitstempel direkt in NTP SHM schreibt und gpsd kein separates
PPS-Device sieht. ntpd/chrony erhalten die PPS-Daten korrekt ueber SHM Unit 1.

## Samsung UCCM - Protokoll-Details

### NMEA-Saetze (generiert durch Bridge)

Die Bridge erzeugt aus den SCPI-Daten folgende NMEA-0183-Saetze in dieser
Reihenfolge pro Sekunde:

| Satz      | Inhalt                              | Hinweis                        |
|-----------|-------------------------------------|--------------------------------|
| `$GPRMC`  | Zeit, Position, Status              | Cycle-Starter (gpsd)           |
| `$GPZDA`  | UTC-Datum und -Zeit                 | Praezise Zeitquelle fuer NTP   |
| `$GPGSA`  | Fix-Typ, aktive Satelliten (PRNs)   | gpsmon zeigt Satellitenliste   |
| `$GPGGA`  | Fix-Qualitaet, Satellitenzahl, Hoehe| Cycle-Ender (gpsd)             |

`$GPGSV` wird nicht erzeugt: das UCCM-SCPI-Interface liefert keine
Elevation/Azimut/SNR-Daten pro Satellit. gpsd 3.25 wuerde Saetze mit
allen Azimuten = 0 als ungueltig ablehnen (SiRFstar-Workaround).

### SCPI-Befehle

| Befehl | Antwort-Format | Polling-Intervall |
|---|---|---|
| `TIME:STRing?` | `YYYY/MM/DD HH:MM:SS` | 1 Hz |
| `GPS:POSition?` | `N,+DD,+MM,+SS.sss,E,+DD,+MM,+SS.sss,+HHH.hh` | alle 30 s |
| `GPS:SATellite:TRACking:COUNt?` | `+N` | alle 30 s |
| `GPS:SATellite:TRACking?` | `+prn,...,+prn` | alle 30 s |
| `LED:GPSLock?` | `Locked` / `Unlocked` | alle 30 s |
| `SYNChronization:TFOMerit?` | z.B. `1 ~ 10 nsec` | alle 30 s |
| `TOD EN` / `TOD DI` | - | einmalig beim Start (`--pps-source tod`) |

### TOD-Pakete (1PPS via `--pps-source tod`)

44 Bytes binaer, hex-kodiert in ASCII, ein Paket pro Sekunde.
Sync-Byte: `0xC5`. Byte 30 enthaelt die Sekunde im BCD-Format.

## Lizenz

MIT

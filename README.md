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
  ├── SCPI-Polling (TIME:STRing?, GPS:POSition?, ...)  ── 1 Hz
  ├── TOD-Datenstrom (1PPS-Pakete via TOD EN)          ── 1x/s
  ├── Log-Abfragen (separate TCP-Verbindung)           ── auf Anfrage
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS-Clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### Serieller Modus (--serial)

```
Samsung UCCM GPS
        | /dev/ttyUSB0 (seriell, SCPI-CLI)
uccm_scpi_bridge.py
  ├── SCPI-Polling (TIME:STRing?, GPS:POSition?, ...)  ── 1 Hz
  ├── DCD-Pin-Polling (1PPS via steigende Flanke)      ── 1 ms Intervall
  ├── PTY /dev/uccm_gps  ──────► gpsd ──► GPS-Clients
  └── NTP SHM Unit 0/1   ──────► ntpd / chrony
```

### 1PPS-Quellen (`--pps-source`)

| `--pps-source` | TCP-Modus          | Serieller Modus        | Typischer Jitter SHM1 |
|----------------|--------------------|------------------------|-----------------------|
| `auto`         | TOD-Pakete         | DCD-Pin                | TCP: 100–700 ms / DCD: < 2 ms (¹) |
| `tod`          | TOD-Pakete         | TOD-Pakete             | TCP: 100–700 ms / seriell: ~5 ms |
| `dcd`          | — (Warnung)        | DCD-Pin                | < 2 ms (¹)            |
| `none`         | kein 1PPS          | kein 1PPS              | inaktiv               |

(¹) Nur mit FTDI-Latency-Timer = 1 ms (siehe Abschnitt unten). Standardwert
16 ms ergibt bis zu 16 ms zusaetzlichen Jitter auf DCD-basierten PPS-Messungen.

**Empfehlung:** Fuer PPS-Qualitaet (< 5 ms Jitter) seriellen Modus mit `auto`
(= DCD-Pin, 1 ms Polling) und FTDI-Low-Latency verwenden. TOD ueber TCP
unterliegt variablem Netzwerk- und Scheduling-Jitter (100–700 ms) und ist als
primaere PPS-Quelle fuer NTP nicht empfehlenswert — bei Verwendung von TOD
TCP den NTP-Offset mit `fudge time2` (ntpd) bzw. `offset` (chrony) anpassen
(typischer Wert: gemessenen SHM1-Offset aus `ntpq -p` / `chronyc sources`
mit umgekehrtem Vorzeichen).

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

## FTDI USB-Serial: Low-Latency-Modus (serieller Modus)

Bei Verwendung eines FTDI-basierten USB-Serial-Adapters (z.B. `/dev/ttyUSB0`)
puffert der FTDI-Chip eingehende Daten standardmaessig fuer bis zu **16 ms**
(Latency Timer), bevor er sie per USB an den Host uebertraegt. Das betrifft
auch Modem-Status-Events wie DCD-Flanken und verursacht direkt **bis zu 16 ms
Jitter** auf SHM Unit 1 beim `--pps-source dcd`-Modus.

Latency Timer auf 1 ms setzen (als root, nicht persistent):

```bash
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
# Kontrollieren:
cat /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

Persistent via udev-Regel (`/etc/udev/rules.d/99-ftdi-latency.rules`):

```
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
```

Regel aktivieren:

```bash
sudo udevadm control --reload-rules
# Adapter neu einstecken oder:
sudo udevadm trigger --subsystem-match=usb-serial
```

**Hinweis:** Der FTDI-Latency-Timer ist nur fuer den seriellen Modus
(`--serial`) mit DCD-basiertem 1PPS relevant. Im TCP-Modus (TOD-Pakete)
spielt er keine Rolle.

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

# Serieller Modus, 1PPS via DCD-Pin (Standard, geringster Jitter)
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --baud 9600

# Serieller Modus, 1PPS via TOD-Datenstrom statt DCD
sudo ./uccm_scpi_bridge.py --ntp-shm --serial /dev/ttyUSB0 --pps-source tod

# Mit Web-Interface
sudo ./uccm_scpi_bridge.py --ntp-shm --web-port 8080 192.168.1.100 2000

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
| `--web-port`        | `0`             | HTTP-Status-Port (0 = deaktiviert, z.B. `8080`)        |
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

### NTP SHM Timing

| SHM Unit | Inhalt | clockTime | receiveTime | Offset |
|----------|--------|-----------|-------------|--------|
| 0 (GPS)  | GPS-Sekunde (TIME:STRing?) | GPS-Sekunde (ganz) | Systemzeit nach SCPI-Antwort | ≈ −(SCPI-Latenz, ~200–400 ms) |
| 1 (PPS)  | TOD-Paket oder DCD-Flanke | GPS-Sekunde (BCD) | Systemzeit bei Empfang | abhaengig von Quelle (s.u.) |

**SHM0 (GPS NMEA):** Der Offset ist negativ und entspricht der SCPI-Abfragelatenz
(typisch 200–400 ms). NTP erlernt diesen Wert automatisch. Optional kann er mit
`fudge time1` kompensiert werden — den genauen Wert aus `ntpq -p` (Spalte
`offset`) ablesen und mit umgekehrtem Vorzeichen eintragen.

**SHM1 (1PPS):** Der Jitter und der systematische Offset haengen von der PPS-Quelle ab:

| PPS-Quelle | Jitter | System. Offset | Empfehlung |
|------------|--------|----------------|------------|
| DCD seriell (FTDI latency=1) | < 2 ms | < 1 ms | Empfohlen als primaere PPS-Quelle |
| DCD seriell (FTDI latency=16) | bis 16 ms | < 8 ms | Akzeptabel, aber FTDI-Latency anpassen |
| TOD seriell | ~5 ms | < 5 ms | Akzeptabel; UART-Uebertragung ist deterministisch |
| TOD TCP | 100–700 ms | 100–500 ms | Nicht als primaere PPS-Quelle; `fudge time2` / `offset` eintragen |

Bei TOD TCP: den gemessenen SHM1-Offset aus `ntpq -p` oder `chronyc sources`
(Spalte `offset`) mit **umgekehrtem Vorzeichen** als `fudge time2` (ntpd) bzw.
`offset` (chrony) eintragen. Damit kompensiert NTP den fixen Verzoegerungsanteil.

**Offset-Wert ermitteln (TOD TCP):**

```bash
# ntpd: Offset in Spalte "offset" ablesen (in ms), Vorzeichen umkehren, in s umrechnen
ntpq -p
# Beispiel: SHM(1) zeigt offset = -195.000 ms  →  time2 = +0.195

# chrony: Offset in Spalte "Offset" ablesen
chronyc sources -v
# Beispiel: SHM(1) zeigt -195 ms  →  offset = +0.195
```

Dieser Wert ist typischerweise stabil (< 20 ms Variation) und muss einmalig
gemessen und eingetragen werden. Nach dem Neustart von ntpd/chrony mit dem
korrigierten Wert sollte SHM1 einen Offset < 20 ms aufweisen und als valide
Quelle akzeptiert werden.

### ntpd (`/etc/ntp.conf`)

```
# GPS NMEA (SHM Unit 0) - Grobgenauigkeit ~0,5 s, Offset durch SCPI-Latenz
server 127.127.28.0 minpoll 4 maxpoll 4
fudge  127.127.28.0 refid GPS time1 0.3
# time1: gemessenen offset-Wert aus "ntpq -p" mit umgekehrtem Vorzeichen eintragen

# 1PPS via TOD/DCD (SHM Unit 1) - Feingenauigkeit (DCD/TOD seriell empfohlen)
server 127.127.28.1 minpoll 4 maxpoll 4 prefer
fudge  127.127.28.1 refid PPS
# time2: nur bei TOD TCP noetig (systematischer Delay-Offset)
# Wert = gemessenen SHM1-Offset aus "ntpq -p" mit umgekehrtem Vorzeichen, z.B.:
# fudge  127.127.28.1 refid PPS time2 0.2

# Empfehlung: Fallback-Zeitserver eintragen, damit die Systemuhr nicht driftet
# wenn GPS-Quellen als Falseticker markiert werden:
# server pool.ntp.org iburst
```

### chrony (`/etc/chrony.conf`)

```
# GPS NMEA (SHM 0) - offset: gemessenen Wert aus "chronyc sources" anpassen
refclock SHM 0 refid GPS precision 1e-1 offset 0.3 poll 4
# offset: positiver Wert = SCPI-Latenz in Sekunden (typisch 0.2-0.4)

# 1PPS via TOD/DCD (SHM 1) - DCD oder TOD seriell empfohlen
refclock SHM 1 refid PPS precision 1e-9 prefer poll 4
# offset: nur bei TOD TCP noetig (systematischer Delay-Offset)
# Wert = gemessenen SHM1-Offset aus "chronyc sources" mit umgekehrtem Vorzeichen, z.B.:
# refclock SHM 1 refid PPS precision 1e-9 prefer poll 4 offset 0.2

# Empfehlung: Fallback-Zeitserver eintragen, damit die Systemuhr nicht driftet
# wenn GPS-Quellen als Falseticker markiert werden:
# pool pool.ntp.org iburst
```

### Hinweis: Raspberry Pi ohne Hardware-RTC

Raspberry-Pi-Systeme ohne Hardware-Echtzeituhr (RTC) laden beim Boot einen
gespeicherten Zeitstempel aus `fake-hwclock`. Wenn dieser Zeitstempel weit
von der tatsaechlichen UTC-Zeit abweicht, lernt NTP eine falsche Oszillator-
Frequenz und schreibt einen ungueltigen Wert in die Drift-Datei. Beim
naechsten Boot wird dieser Wert sofort geladen — was dazu fuehren kann, dass
die Frequenzkorrektur das NTPd-Hard-Limit von **±500 PPM** erreicht. In
diesem Zustand driftet die Systemuhr unkontrolliert, GPS- und PPS-Quellen
zeigen grosse Offsets und werden als Falseticker verworfen.

**Symptom:** `ntpq -c rv` zeigt `frequency=-500.0` (oder `+500.0`).

**Sofortreparatur:**

```bash
sudo systemctl stop ntpsec
sudo truncate -s 0 /var/lib/ntpsec/ntp.drift   # oder /var/lib/ntp/ntp.drift
sudo systemctl start ntpsec
# 15–30 Minuten warten, bis NTP die korrekte Frequenz neu erlernt hat
```

**Beschleunigtes initiales Sync:** `tos minsane 1 minclock 1` zusammen mit
`iburst` in `ntp.conf` erlaubt NTP, sich bereits nach einem einzigen
erreichbaren Server zu synchronisieren, statt auf eine Mehrheitsquote zu
warten:

```
tos minsane 1 minclock 1
server ptbtime1.ptb.de iburst
server ptbtime2.ptb.de iburst
server ptbtime3.ptb.de iburst
```

**`fake-hwclock`:** Unter Raspberry Pi OS ist `fake-hwclock` standardmaessig
in `cron.hourly` eingetragen und speichert die Systemzeit stuendlich. Dadurch
weicht der gespeicherte Zeitstempel beim naechsten Boot maximal ~1 Stunde
von der tatsaechlichen UTC-Zeit ab — was NTP problemlos ausgleichen kann,
ohne eine fehlerhafte Drift-Datei zu erzeugen.

## Web-Interface

Mit `--web-port 8080` startet die Bridge einen integrierten HTTP-Server:

```bash
sudo ./uccm_scpi_bridge.py --ntp-shm --web-port 8080 192.168.1.100 2000
```

Aufruf im Browser: `http://<bridge-host>:8080/`

Die Seite aktualisiert sich alle 2 Sekunden automatisch und zeigt:

| Feld           | Beschreibung                                 |
|----------------|----------------------------------------------|
| Verbindung     | TCP/seriell verbunden oder getrennt          |
| GPS Lock       | Locked / Unlocked                            |
| GPS-Zeit       | Letzter UTC-Zeitstempel vom Geraet           |
| Position       | Breite / Laenge / Hoehe                      |
| Satelliten     | Anzahl + PRN-Liste                           |
| TFOM           | Zeitgenauigkeit (SYNChronization:TFOMerit?)  |
| ANT-Spannung   | Antenne: Versorgungsspannung                 |
| ANT-Strom      | Antenne: Stromaufnahme                       |
| Temperatur     | Geraetetemperatur                            |
| 1PPS-Quelle    | Konfigurierte PPS-Quelle (auto/tod/dcd/none) |
| Letzter PPS    | Zeitstempel des letzten 1PPS-Impulses        |
| Bridge-Start   | Startzeitpunkt der Bridge                    |

ANT-Spannung, ANT-Strom und Temperatur werden alle 30 Sekunden aus
`SYSTEM:STATUS?` ausgelesen.

### Diagnose-Log

Ueber die Web-Oberflaeche kann das interne UCCM-Diagnose-Log abgerufen
und geloescht werden:

- **Log laden** – liest das Log via `DIAGNOSTIC:LOG:READ:ALL?`
- **Log loeschen** – loescht das Log via `DIAGNOSTIC:LOG:CLEAR`

Im TCP-Modus laufen Log-Operationen ueber eine **separate TCP-Verbindung**,
sodass der SCPI-Polling-Loop (und damit NTP SHM) nicht unterbrochen wird.

### HTTP-API

| Endpunkt         | Methode | Beschreibung                              |
|------------------|---------|-------------------------------------------|
| `/`              | GET     | Web-Interface (HTML)                      |
| `/status`        | GET     | Bridge-Status als JSON                    |
| `/log`           | GET     | Diagnose-Log als JSON (`{"lines": [...]}`) |
| `/log/clear`     | POST    | Diagnose-Log loeschen                     |

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
| `SYSTEM:STATUS?` | Mehrzeiliger Statusblock (ANT, Temp, ...) | alle 30 s |
| `DIAGNOSTIC:LOG:READ:ALL?` | Zeilenweise Log-Eintraege | auf Anfrage (Web-UI) |
| `DIAGNOSTIC:LOG:CLEAR` | — | auf Anfrage (Web-UI) |
| `TOD EN` / `TOD DI` | — | einmalig beim Start (`--pps-source tod`) |

### TOD-Pakete (1PPS via `--pps-source tod`)

44 Bytes binaer, hex-kodiert in ASCII, ein Paket pro Sekunde.
Sync-Byte: `0xC5`. Byte 30 enthaelt die Sekunde im BCD-Format (0x00–0x59).

Die Bridge verwendet das BCD-Sekundenfeld zur Plausibilitaetspruefung und
korrigiert `gps_sec` um ±1 s, falls das Paket nahe an einer Sekundengrenze
eingetroffen ist und `recv_time` auf die falsche Sekunde rundet.

## Lizenz

MIT

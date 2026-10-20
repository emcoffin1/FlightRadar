# FlightRadar / ADS-B Display

This Flask application uses a local `readsb` receiver by default and can
optionally retrieve from and feed to adsb.lol. The browser-facing aircraft APIs
remain the same regardless of source.

## Run the application

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

Open `http://<radar-host>:5000`. Use **Coords** to set the receiver latitude
and longitude, enable the local receiver, choose its address, and enable or
disable adsb.lol integration.

The receiver address accepts any of these forms:

- `/run/readsb/aircraft.json` when Flask and `readsb` run on the same computer.
- `http://pi-zero:8080/aircraft.json` when JSON is exposed by the receiver.
- `pi-zero` or `192.168.1.20`, which resolves to
  `http://<address>/readsb/aircraft.json`.

Local receiver JSON is enabled by default and polled every 1 second. When the
**Use adsb.lol** toggle is enabled, the public point API is polled every 30
seconds and local Beast data is uploaded. The local and public snapshots are
merged by ICAO hex; fresh local position data wins while public metadata fills
missing fields.

## Install `readsb` on the receiver

The receiver computer is not assumed to be preconfigured. On a Debian/Ubuntu
system where the package is available:

```bash
sudo apt update
sudo apt install readsb
sudoedit /etc/default/readsb
```

Use the tested options below, replacing the coordinates with the antenna's
actual location:

```text
RECEIVER_OPTIONS="--device 0 --device-type rtlsdr --gain 43.9 --ppm 0"
DECODER_OPTIONS="--max-range 450 --write-json-every 1 --lat YOUR_LAT --lon YOUR_LON"
```

Then enable and verify the decoder:

```bash
sudo systemctl enable --now readsb.service
systemctl status readsb.service
ls -l /run/readsb/aircraft.json
```

### Local aircraft type and registration metadata

Aircraft generally do not broadcast their model or registration. To add the
type, registration, and military flags directly in FlightRadar, download the
compatible local database into the repository:

```bash
curl -fL \
  https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz \
  -o data/aircraft.csv.gz
```

FlightRadar imports this file into its local SQLite database and enriches
receiver contacts by ICAO hex. No external request is made during lookup.

You can also configure `readsb` itself to add the `t`, `r`, and `dbFlags`
fields to `aircraft.json`. Install the same database in its standard location:

```bash
sudo install -d /usr/local/share/tar1090
sudo curl -fL \
  https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz \
  -o /usr/local/share/tar1090/aircraft.csv.gz
gzip -t /usr/local/share/tar1090/aircraft.csv.gz
```

Add the database options to `DECODER_OPTIONS` in `/etc/default/readsb`:

```text
--db-file /usr/local/share/tar1090/aircraft.csv.gz --db-file-lt
```

Keep the existing decoder options on the same line, then restart and verify:

```bash
sudo systemctl restart readsb.service
python3 -c 'import json; d=json.load(open("/run/readsb/aircraft.json")); print([(a.get("hex"), a.get("r"), a.get("t")) for a in d.get("aircraft", [])])'
```

This lookup stays on the receiver after the database file is downloaded. The
database should be refreshed periodically to pick up new registrations.

The tested Ubuntu package places `readsb` at `/usr/bin/readsb`, writes live JSON
to `/run/readsb/aircraft.json`, and provides Beast output on TCP port 30005.

### Backend on another computer

When Flask runs on a Pi 4B or another host, expose only the receiver services
needed on the trusted LAN:

1. Serve `/run/readsb/aircraft.json` with a small web server or existing
   nginx/lighttpd configuration, then enter its full URL in the Coords dialog.
   For a temporary LAN-only test, for example:

   ```bash
   python3 -m http.server 8080 --directory /run/readsb --bind 0.0.0.0
   ```

   Then use `http://RECEIVER_IP:8080/aircraft.json`. Configure a supervised
   web server for permanent deployment.
2. If adsb.lol feeding will be enabled from the Flask host, make Beast TCP port
   30005 reachable. A typical readsb network setting is:

   ```text
   NET_OPTIONS="--net --net-bind-address=0.0.0.0 --net-bo-port=30005"
   ```

3. Restart `readsb` after configuration changes:

   ```bash
   sudo systemctl restart readsb.service
   ```

Restrict the JSON web port and TCP 30005 to the local network; do not expose
them directly to the internet.

## Optional adsb.lol integration

The **Use adsb.lol** switch is in the Coords dialog. When enabled, the app
retrieves public aircraft every 30 seconds. When the local receiver is also
enabled, it starts a `readsb --net-only` relay from the configured Beast
host/port to adsb.lol. A stable station UUID is stored in
`.runtime/adsblol_station_uuid`, and relay output is appended to
`.runtime/adsblol-feeder.log`.

The Flask backend itself must have `readsb` installed even when the USB receiver
is attached to a different computer. Disabling either switch stops the relay;
disabling **Use adsb.lol** also stops public API polling. Integration is off by
default.

## Aircraft filters

The Normal and Military buttons filter both local and public aircraft. Local
military identification depends on `dbFlags` being present in `readsb` output;
aircraft without the Military flag are treated as normal. Select the 50 NM
range for the intended nearby military-aircraft view.


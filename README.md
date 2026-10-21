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
and longitude, choose its address, and control each local or external feature
independently.

The receiver address accepts any of these forms:

- `/run/readsb/aircraft.json` when Flask and `readsb` run on the same computer.
- `http://pi-zero:8080/aircraft.json` when JSON is exposed by the receiver.
- `pi-zero` or `192.168.1.20`, which resolves to
  `http://<address>/readsb/aircraft.json`.

Local receiver JSON is enabled by default and polled every 1 second. Public
adsb.lol retrieval uses `ADSB_LOL_POLL_SECONDS` in `config.py` (currently 10
seconds). The local and public snapshots are merged by ICAO hex; fresh local
position data wins while public metadata fills missing fields.

## Network and kiosk settings

The coordinate dialog persists these independent settings in `aircraft.db`:

- **Use local receiver** reads the configured local/LAN `aircraft.json`.
- **Upload to adsb.lol** relays local Beast data; it does not enable retrieval.
- **Remote adsb.lol aircraft** polls the public point API; it does not upload.
- **External route lookup** permits background ADSBDB/HexDB lookups.
- **Download map tiles** permits OpenStreetMap cache misses and revalidation.
- **Offline mode** overrides every external option without disabling local data.
- **Low-resource kiosk mode** disables continuous marker extrapolation and
  expensive map/icon filters while retaining one-second observations.

Offline mode continues to use cached tiles. Uncached areas render a neutral
blank tile rather than causing an external request. Local assets and aircraft
metadata remain available.

On migration, the old combined adsb.lol value is copied to the new upload and
remote-poll settings to preserve that explicit choice. Route lookups and map
downloads are opt-in; this avoids adding hidden traffic to local-first systems.
The legacy database column and API names remain for compatibility.

## Receiver installation

The deployed Raspberry Pi uses the existing `/usr/local/bin/readsb`, built
from upstream's `stale` branch with `make -j1 RTLSDR=yes`. Keep the service
override pointing to that binary. Do not run an automatic receiver installer
or replace this working build while deploying FlightRadar changes.

For a new Debian/Ubuntu receiver only, a packaged installation can be tried:

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

Uploading and remote retrieval have separate switches in the Coords dialog.
When upload and the local receiver are enabled, the app starts a
`readsb --net-only` relay from the configured Beast host/port to adsb.lol. A
stable station UUID is stored in
`.runtime/adsblol_station_uuid`, and relay output is appended to
`.runtime/adsblol-feeder.log`.

The Flask backend itself must have `readsb` installed even when the USB receiver
is attached to a different computer. Disabling upload, the local receiver, or
enabling offline mode stops the relay. Integration is off by default.

## Resource bounds and diagnostics

Aircraft observations remain live at one-second cadence. Trail samples are
recorded at most every five seconds, retained for one hour, pruned in batches,
and limited to 360 returned points. Route work uses two background workers and
a bounded queue. Successful and unavailable routes are cached by normalized
callsign; transient failures back off instead of retrying on every detail poll.

`GET /api/diagnostics` reports current aircraft/trail/cache sizes, external
request outcomes, and route queue/cache/backoff counts. The browser prints one
small frontend timing summary to its console per minute rather than logging
every refresh.

## Raspberry Pi deployment and validation

These changes do not modify `readsb.service` or its receiver binary. After
placing the updated repository on the Pi:

```bash
cd /path/to/FlightRadar
. .venv/bin/activate
python3 -m unittest discover -s tests -v
python3 -m py_compile app.py adsblol.py database.py route_enrichment.py temp.py
sudo systemctl restart adsb-radar.service
sudo systemctl restart flight-kiosk.service
systemctl --no-pager --full status adsb-radar.service flight-kiosk.service
curl -fsS http://127.0.0.1:5000/api/diagnostics
```

For a rollback, preserve the current working tree as a patch before copying or
pulling changes, then reverse that exact patch and restart only the two
application services:

```bash
git diff --binary > ../flightradar-before-performance.patch
# To reverse the applied performance patch later:
git apply --reverse /path/to/performance.patch
sudo systemctl restart adsb-radar.service flight-kiosk.service
```

First run a short comparison with similar aircraft counts, watching `vmstat 1`,
zram use, touch response, and diagnostics. Then run a multi-hour soak on the
same display. Desktop/unit tests cannot establish Pi/Cog memory or rendering
behavior; browser footprint must settle and swap/I/O churn must be checked on
the actual Zero 2 W.

## Aircraft filters

The Normal and Military buttons filter both local and public aircraft. Local
military identification depends on `dbFlags` being present in `readsb` output;
aircraft without the Military flag are treated as normal. Select the 50 NM
range for the intended nearby military-aircraft view.


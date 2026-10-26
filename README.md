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
metadata is preferred when present, while the freshest valid position wins.

Remote requests run in one bounded background worker. A DNS failure, timeout,
rate limit, authorization failure, or slow 15-second HTTP request therefore
does not block the one-second local receiver loop. Only one remote request can
be active, and results from obsolete coordinates/settings generations are
discarded.

## Network and kiosk settings

The coordinate dialog persists these independent settings in `aircraft.db`:

- **Use local receiver** reads the configured local/LAN `aircraft.json`.
- **Upload to adsb.lol** relays local Beast data; it does not enable retrieval.
- **Remote adsb.lol aircraft** polls the public point API; it does not upload.
- **Remote fallback when local is empty** enables the 15-second fallback
  policy without enabling continuous remote collection.
- **Remote endpoint** selects `auto`, the public point API, or feeder-only
  re-api. `auto` prefers re-api only when local reception and this app's upload
  relay are both enabled. Selecting `feeder` supports a feed managed elsewhere.
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
Migration adds the endpoint preference as `auto` without enabling upload or
remote retrieval and without changing existing network choices.

### Position and trail freshness

All stored observation times are absolute UTC Unix seconds. Scheduling uses a
monotonic clock. The initial centralized policy in `config.py` is:

- up to 5 seconds future-clock tolerance;
- source snapshots no older than 15 seconds;
- messages and precise positions considered fresh for 20 seconds;
- collector heartbeat considered healthy for 10 seconds;
- remote snapshots cached for at most 30 seconds;
- aircraft metadata retained for five minutes after its last message;
- a new trail segment after a 120-second gap or an implied speed over 1500 kt.

For native readsb data, message and position observation times are derived from
the snapshot's `now` plus `seen` and `seen_pos`. Polling or writing SQLite never
renews those times. A recent message cannot refresh old coordinates.
`lastPosition` is historical and `rr_lat`/`rr_lon` are rough receiver-range
estimates; neither is promoted to a live marker. The API also requires a fresh
collector heartbeat, so Flask cannot serve stopped-collector records as live.

Only the latest continuous trail segment is drawn. Historical database points
remain bounded to one hour/360 points, but the UI will not draw a straight line
across a long reception gap or implausible jump.

## Receiver installation

The deployed Raspberry Pi uses the existing `/usr/local/bin/readsb`, built
from upstream's `stale` branch with `make -j1 RTLSDR=yes`. Keep the service
override pointing to that binary. Do not run an automatic receiver installer
or replace this working build while deploying FlightRadar changes. Its working
aircraft database is `/usr/local/share/readsb/aircraft.csv.gz`; leave that file
and `/etc/default/readsb` untouched during an application deployment.

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
Aircraft reference photographs are also resolved locally by ICAO type code;
the browser never calls an aircraft-photo API. The bundled family mappings
currently cover about 71% of typed records in the bundled metadata snapshot,
including common Beechcraft Baron/BE95, Mooney, Diamond, Cessna twin, glider,
balloon, ultralight, Van's RV, Piper, Bell and King Air codes. Photographs are
representative family images, not pictures of the particular live airframe.

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

The feeder-only re-api is the network's native readsb query endpoint and is
requested geographically with `?circle=LATITUDE,LONGITUDE,RADIUS_NM`. Its
documented access path authorizes the feeder station's public IP. A station UUID
is not an API credential. adsb.lol advertises receiver API keys, but its public
documentation does not specify an authentication header or parameter, so this
application does not guess one. Provider selection is shared by continuous and
fallback acquisition.

All remote requests originate from the Flask backend. Viewing through Tailscale
does not move those requests to the phone, although an exit node, different
internet connection, or IPv4/IPv6 routing can change the backend's public IP and
therefore feeder authorization. The re-api supplies aircraft observations, not
routes/photos or a local MLAT installation.

Retrieved re-api data is documented as ODbL 1.0. Data sent to the adsb.lol feed
endpoints is covered by the service's separate CC0 upload terms. See the
[re-api documentation](https://www.adsb.lol/docs/feeders-only/re-api/) and
[privacy/license notice](https://www.adsb.lol/docs/privacy-license).

## Resource bounds and diagnostics

Aircraft observations remain live at one-second cadence. Trail samples are
recorded at most every five seconds, retained for one hour, pruned in batches,
and limited to 360 returned points. The selected aircraft's bounded detail and
trail request runs every seven seconds, pauses while the page is hidden, and is
independent of route status. Route work uses two background workers and a
bounded queue. Successful and unavailable routes are cached by normalized
callsign; transient failures back off instead of retrying on every detail poll.
Queued route work is invalidated when offline mode or route enrichment is
disabled. An HTTP request already in progress may finish within its 15-second
timeout, but a fallback provider is not started after permission is revoked.

Malformed, expired, missing, or non-positive adsb.lol `Retry-After` values use
a 60-second minimum delay. HTTP dates are converted with the wall clock before
the duration is scheduled against the monotonic clock. Remote HTTP work runs in
its own single-worker executor, so a slow request does not delay the local
one-second loop. An already-running remote request may still take up to its
15-second timeout to finish after its setting is disabled; its obsolete result
is rejected.

`GET /api/diagnostics` reports current aircraft/trail/cache sizes, external
request outcomes, collector heartbeat/receiver freshness, active remote
provider and authorization status, snapshot ages/expiry, rejected positions,
live/stale/historical counts, upload-relay state, route queue/cache/backoff,
trail revision/segments/points, and rate-limited frontend detail failures. The
browser prints and reports one small timing summary per minute rather than
logging every refresh.

## Raspberry Pi deployment and validation

These changes do not modify `readsb.service` or its receiver binary. Before
switching revisions, record the current state and make a consistent SQLite
backup while the application service is stopped:

```bash
cd /path/to/FlightRadar
git rev-parse HEAD
git status --short
git diff --binary > ../flightradar-before-upgrade.patch
sudo systemctl stop adsb-radar.service
sqlite3 aircraft.db '.backup ../aircraft-before-upgrade.db'
```

Switch to the reviewed commit, run its migrations/tests by starting it from the
virtual environment, then restart only the application services. Leave
`readsb.service`, `/etc/default/readsb`, and `/usr/local/bin/readsb` unchanged:

```bash
git switch --detach <review-commit>
. .venv/bin/activate
python3 -m unittest discover -s tests -v
python3 -m py_compile app.py adsblol.py database.py freshness.py \
  local_receiver.py route_enrichment.py temp.py
sudo systemctl restart adsb-radar.service
sudo systemctl restart flight-kiosk.service
systemctl --no-pager --full status adsb-radar.service flight-kiosk.service
curl -fsS http://127.0.0.1:5000/api/diagnostics
```

For a rollback, stop the application, restore both the recorded commit and the
pre-migration database, then restart only the two application services:

```bash
sudo systemctl stop adsb-radar.service
git switch --detach <previous-commit>
cp ../aircraft-before-upgrade.db aircraft.db
sudo systemctl restart adsb-radar.service flight-kiosk.service
```

First run offline/local-only with constrained mode enabled. Verify aircraft
movement, trail advancement, departures, full-height navigation touch targets,
and stale removal by briefly stopping `readsb.service` while leaving the radar
backend running. Restart readsb immediately after observing the stale
transition. Then enable remote retrieval and test re-api from the Pi's actual
network connection with the documented `curl --max-time 20` request. Compare
diagnostics before/after failures and verify local timestamps continue to
advance.

Watch `free -h`, `swapon --show`, `zramctl`, and interval `vmstat 2` samples,
while comparing touchscreen response with a phone view. Follow the supervised
run with a multi-hour soak. Unit tests cannot establish Pi/Cog memory, Safari
layout, re-api authorization, or rendering behavior; these remain hardware and
network acceptance checks.

## Aircraft filters

The Normal and Military buttons filter both local and public aircraft. Local
military identification depends on `dbFlags` being present in `readsb` output;
aircraft without the Military flag are treated as normal. Select the 50 NM
range for the intended nearby military-aircraft view.

## Basic display

The application boots into the reversionary Basic display at `/`; `/basic`
remains a direct alias. The **Extra** tab on its left opens the normal radar at
`/extra`, whose curved **Basic** tab returns to the reversionary display. A normal
tap replaces the current page rather than adding map pages to browser back
history. The display reserves room for the tab so it cannot cover controls.

Basic fills the screen with the nearest fresh aircraft's callsign, type,
registration, reference image, departure/destination airport information,
distance, altitude, ground speed and track. It automatically changes contact
as aircraft move, expire or leave the configured range.
Missing values are shown as dashes; stale or disconnected data clears the
contact. The configured range applies to both the normal snapshot and closest
request. Basic has no settings dialog; return to Extra to change its location
or range.

Aircraft acquisition has three explicit policies. **Remote fallback when local
is empty** is enabled by default and requests remote
data only when no fresh, in-range local position passes the Normal/Military
filters. **Remote adsb.lol aircraft** takes precedence when enabled and retains
continuous remote collection. Offline mode blocks both remote policies, while
uploading remains a separate setting.

The fallback is owned by one collector worker shared by Basic and Extra, not by
individual browsers. Successful requests start on a 15-second monotonic cadence
(`ADSB_LOL_CLOSEST_POLL_SECONDS` in `config.py`), with no overlap or catch-up
bursts. Public acquisition uses adsb.lol's closest endpoint when both Normal
and Military are allowed. When filtering is required, or feeder re-api is
selected, it uses the selected geographic provider and deterministically picks
the nearest eligible result. The public v2 millisecond timestamp and feeder
readsb timestamp are normalized before observation ages are calculated.

Fresh local positions immediately cancel fallback eligibility and invalidate
late results. Green **Live** means the displayed coordinates came from the
local receiver; orange means their `position_source` is remote, including
continuous collection. Expired coordinates are removed rather than relabeled.
Pending, empty, stale, rate-limited, unauthorized, and request-error states are
reported without claiming Live data.

Basic loads system fonts and at most one local aircraft reference image: no
Leaflet, tiles, aircraft icons, trails or animation loop. It uses the existing
route-enrichment permission and bounded cache for airport information; when
that permission is off it clearly says so and makes no route request. It requests one small
`/api/aircraft/nearest` response at a time, waits two seconds after completion,
and aborts requests after four seconds. Polling stops while hidden or leaving
the page. An independent expiry timer clears a contact when its position or
collector heartbeat becomes stale, even if the next request hangs.

This switches the browser view only. Collection, optional feeding, configured
remote polling, and history recording continue in the backend. The default
kiosk URL `http://127.0.0.1:5000/` now opens Basic; no receiver changes are
needed.

## Display performance review (October 2026)

Changes in this revision:

- Extra cancels its animation frame loop while hidden, empty, or in constrained
  mode. Previously an unconstrained hidden/empty map still scheduled frames.
- Constrained mode skips motion-state calculations and clears retained motion
  states when enabled. Static marker positions still update at receiver cadence.
- Repeated primary text values no longer rewrite identical DOM text nodes.
- Frontend diagnostics has a five-second timeout and a single-flight guard,
  preventing hung minute-by-minute reports from accumulating requests.
- Basic replaces the heavyweight document instead of hiding a running map.

Existing protections verified in the code: departed marker/motion/list entries
are removed, list rows and marker elements are reused, details have timeout and
generation guards, trail responses are capped at 360 points, route workers have
a bounded queue, remote collection permits one outstanding request, and SQLite
connections close through a context manager. These are not evidence of an
unbounded collection leak. WPE/Cog process memory, decoded images, map tiles,
graphics buffers and zram can still consume much of a Zero 2 W's memory.

Remaining findings:

- `route_cache` has no retention/expiry policy. It can accumulate rows on disk
  and preserve outdated routes for recurring callsigns. A separate change should
  define success/negative-cache TTLs and batched pruning while preserving offline
  cached results. Basic never reads or populates that cache.
- Downloaded map tiles have no global disk budget; changing receiver locations
  can leave older tile files behind. This is disk growth, not a demonstrated RAM
  leak. Basic does not request tiles.
- Extra still polls snapshots every second and renders a map and selected detail
  every seven seconds. Its normal browser/graphics footprint remains significant
  on 512 MB hardware; constrained mode should stay enabled on the Zero 2 W.
- The available logs identify memory pressure and kiosk-specific lag, but do not
  establish that the physical screen is responsible. No JavaScript heap profile
  or WPE/Cog multi-hour memory trace was available for this review.

Validation: `python3 -m unittest discover -s tests -v` includes endpoint tests
and, when Node.js is installed, deterministic Basic-client and animation-loop
tests. They cover stale/empty contacts, nearest switching, no route/asset work,
request timeouts, non-overlap, unchanged text, and hidden/page-exit cleanup.
Existing scheduler tests can also run with `gjs -m tests/detail_scheduler_test.js`.

Before relying on this release on the Pi, compare Extra and Basic at the actual
display resolution, tap both tabs repeatedly, verify empty/stale reception, and
run each mode for at least an hour with similar traffic. Record process RSS,
`free -h`, `zramctl`, and the interval rows of `vmstat 2` (the first row is a
since-boot average). Compare available RAM and swap-in/out activity, rather than
only swap occupancy; inactive swapped pages may remain after pressure subsides.
Browser binaries were unavailable in the review environment, so Safari/DRM visual
layout and actual WPE memory savings still require this hardware check.

## Fallback deployment and hardware acceptance

Deploy application files only; do not replace the working readsb binary,
configuration, service overrides, or aircraft database. After updating the
checkout, restart `adsb-radar.service`, then restart `flight-kiosk.service` if
the browser did not reload the changed assets. Check `/api/diagnostics` for the
acquisition policy, provider, eligible local count, selected position source,
fallback timestamps, state, error, and request count.

On the Pi, select **Remote fallback when local is empty** and turn continuous
**Remote adsb.lol aircraft** off. Confirm fresh local positions show green.
Temporarily stop `readsb.service` to test loss reliably—removing the antenna is
inconclusive because strong signals and recently fresh observations can remain.
Confirm an orange remote contact, no more than one fallback start per 15-second
cadence, and explicit empty/stale/error/backoff states. Restart readsb and
confirm a fresh eligible local position immediately wins and prevents new
fallback starts. During a longer run compare touchscreen response, process RSS,
available memory, `zramctl`, and `vmstat 2` swap activity.

For rollback, stop the application service, check out the previous application
commit, and restart `adsb-radar.service` and `flight-kiosk.service`. The added
SQLite columns are backward-compatible and may remain. No receiver software or
system readsb configuration is changed by this application update.

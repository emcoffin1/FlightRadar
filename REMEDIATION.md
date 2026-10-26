# FlightRadar remediation review

Review date: 2026-10-25

## Scope and branch verification

The requested `freshness-re-api` branch was not present in the local or remote
branch list. The review was performed on
`review/freshness-re-api-navigation` at the requested reviewed commit
`da4dfa453f33151d011ff023eb4abb3ff41a9de9`.

The working readsb installation, receiver configuration, feeder configuration,
and ignored `aircraft.db` were not replaced or reset.

## Reproduced and fixed

### P0: fallback diagnostics were discarded

`Database.update_collector_status()` accepted only the older status fields, so
the collector's acquisition policy and fallback state never reached SQLite.
The schema and persistence layer now share one field definition. Tests persist
and read `requesting`, `remote_fallback`, `stale`, `rate_limited`,
`unauthorized`, `error`, `local`, and `disabled` through
`/api/diagnostics`, `/api/aircraft/nearest`, and the
`X-Acquisition-State` header. Migration coverage starts with a legacy status
table and verifies every supported column is added.

### P1: position fields could describe different observations

An older position was correctly prevented from replacing latitude/longitude,
but its range, altitude, speed, and track were still written. Position-bearing
fields now use one winner decision and are updated atomically. Newer timestamps
win; equal timestamps prefer local reception and otherwise use a deterministic
source order. Invalid, future, or stale coordinates cannot create a live
position. Metadata-only observations retain a newer position, while a newer
authoritative snapshot can still clear a position that genuinely disappeared.

Tests cover older remote data, equal timestamps, local recovery, stale and
invalid coordinates, repeated observations, source disappearance, and trail
segmentation.

### P1: malformed provider records could abort a snapshot

Provider parsing now validates top-level objects and aircraft lists, normalizes
non-string text safely, accepts only finite numeric telemetry, validates
coordinate boundaries and query coordinates, normalizes headings, and rejects
invalid query radii before making a request. A malformed individual aircraft
is skipped or retained as metadata without a live position; it cannot abort
valid neighboring records.

### Lifecycle and cleanup

The directly supervised `python app.py` process is the sole collector owner.
Importing the Flask application does not start a collector, the in-process
guard prevents duplicate threads, and the Werkzeug reloader remains disabled.
The collector now accepts a stop event; normal shutdown stops the feeder and
both remote workers, and the Flask process requests a bounded collector join.
If the collector stops unexpectedly, the existing heartbeat policy makes all
aircraft non-live rather than leaving stale contacts green.

### Collector database cost

The one-second update previously scanned every retained aircraft row before
processing a snapshot. It now queries only incoming ICAO addresses in batches
using a new exact-hex index. A local in-memory SQLite microbenchmark measured:

| Dataset / 200 or 2,000 repeated lookups | Full scan | Indexed lookup | Ratio |
| --- | ---: | ---: | ---: |
| 50,000 rows / 200 lookups | 7,512.7 ms | 14.9 ms | 502.7x |
| 500 rows / 2,000 lookups | 601.4 ms | 132.1 ms | 4.6x |

These are synthetic query-only measurements, not Raspberry Pi end-to-end
collector timings. `EXPLAIN QUERY PLAN` confirms the new query uses
`idx_adsb_data_hex` instead of a table scan.

## Findings verified as already protected

- Local and remote position provenance already flows through
  `position_source`; both displays use it for green/local versus orange/remote.
- API and browser freshness guards already require both a fresh position and a
  fresh collector heartbeat.
- Continuous and fallback acquisition each have one worker, generation checks,
  bounded start-to-start cadence, Retry-After handling, and stale-cache expiry.
- A late fallback result is invalidated when local reception resumes or settings
  change. Cached fallback timestamps are not refreshed when reread.
- Trail insertion rejects duplicate/out-of-order observations and starts a new
  segment after long gaps or implausible jumps.
- Frontend snapshot requests are single-flight, animation stops when hidden or
  constrained, marker/list DOM nodes are reused, and history structures are
  bounded.

## Static assets

The approximately 20 MB `data/` directory supplies the local aircraft and
airport databases. Aircraft icons, photographs, Leaflet, and fonts are mapped
or used at runtime and have accompanying source/license records. No asset was
removed merely for size; doing so would reduce offline coverage or risk losing
required attribution.

## Remaining limitations

- Raspberry Pi/Cog RSS, GPU buffers, swap behavior, and long-duration stability
  still require measurement on the target hardware.
- An unexpected uncaught collector exception makes the heartbeat stale safely,
  but recovery requires the supervised application service to restart.
- Route-cache retention and a global downloaded-tile disk budget remain future
  disk-growth work; neither is an unbounded in-memory refresh structure.
- The full licensed datasets make fresh temporary-database integration tests
  slower because each new database imports aircraft metadata.

## Verification results

- `python3 -m unittest discover -s tests -v`: 94 tests passed; one Node-gated
  wrapper was skipped because `node` is not installed in the shell PATH.
- The skipped Basic wrapper and the animation-loop test were run separately in
  an available Node runtime; both passed.
- Python compilation completed for all production modules.
- `git diff --check` completed without whitespace errors.
- The ignored existing `aircraft.db` migrated in place and reports the new
  `idx_adsb_data_hex` index; no rows or settings were reset.

## Deployment and rollback

Back up the ignored live database and record the current revision before
deployment. Do not replace readsb or `/etc/default/readsb`.

```bash
cd /path/to/FlightRadar
git rev-parse HEAD
sudo systemctl stop adsb-radar.service
sqlite3 aircraft.db '.backup ../aircraft-before-remediation.db'
sudo systemctl start adsb-radar.service
sudo systemctl restart flight-kiosk.service
```

Verify `/api/diagnostics`, local green contacts, fallback orange contacts, and
local recovery. To roll back, stop `adsb-radar.service`, check out the recorded
commit, restore the database backup, and restart the two application services.
The status-column and index migrations are backward-compatible and may also be
left in place.

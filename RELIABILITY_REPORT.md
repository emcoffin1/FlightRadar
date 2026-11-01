# FlightRadar reliability root-cause report

Reviewed base: `2769575411eb0784a1c49323bafa016116e9c3fa`.

## Confirmed defects

- `auto` selected feeder-only re-api whenever upload was enabled. Upload relay
  connectivity does not grant re-api access, so a backend public IP not approved
  by adsb.lol returned HTTP 401/403 and surfaced as `unauthorized`.
- Remote HTTP 200 responses validated aircraft ages but did not reject an old
  top-level provider timestamp. A filtered empty result could then be mistaken
  for a fresh empty sky.
- Reused remote results were not revalidated at the polling boundary on every
  collector pass.
- Source selection lacked explicit empty/failure/recovery hysteresis and could
  not accurately describe valid empty local data versus receiver failure.
- The collector daemon thread could exit while Flask continued serving pages.
- Live aircraft responses lacked explicit browser no-store behavior.
- The feeder subprocess log grew without an application-level bound. Residual
  quiet-mode child output is now discarded and lifecycle errors use journald.

## Findings that differed from the initial review

- The current code already separated continuous remote collection from the
  closest-aircraft fallback. Remote polling was not universally unconditional;
  the primary policy problem was the lack of a centralized state machine and
  the feeder authorization assumption.
- The closest endpoint already uses adsb.lol's closest request directly. It does
  not replace it with a radius request followed by a local nearest calculation.

## Probable deployment cause of `unauthorized`

The code maps HTTP 401 or 403 to `unauthorized`. Before this remediation, Auto
selected feeder re-api when uploading. That makes an unapproved outbound Pi IP
the most likely cause. The historical feeder log only proves that the outbound
upload connection opened; it does not prove authorization to query re-api.

## Requires Raspberry Pi evidence

- The exact failing endpoint, HTTP status, response headers, and outbound public
  IP at the time of failure.
- Whether adsb.lol has authorized that public IP for feeder re-api.
- Actual deployed coordinates/radius versus the readsb station coordinates.
- Provider payload schema and top-level `now`, `seen`, and `seen_pos` ages.
- readsb snapshot age, systemd state, network reachability, and 24-hour resource
  measurements.

Use `/api/diagnostics`, `/api/health/ready`, and the commands in
`deploy/README.md`. Do not include credentials or station secrets in logs.

## Compatibility

Schema migration only adds collector diagnostic columns. Existing remote consent
is preserved. New databases default fallback retrieval off; migrations do not
silently enable external traffic. Existing databases that already enabled it
remain enabled. SQLite now uses WAL, a five-second busy timeout, and normal
synchronous mode for the collector/web concurrency pattern.

## Effective network-request policy

| Setting combination | Aircraft network behavior |
|---|---|
| Offline mode on | No upload, remote aircraft, route, or tile requests |
| Local on; fallback/continuous off | Read local JSON only |
| Upload on | Beast relay only; does not enable retrieval |
| Fallback on; qualifying local aircraft present | No fallback API request |
| Fallback on; three valid empty snapshots or sustained receiver failure | One closest request at a time, nominally every 15 seconds |
| Continuous remote on | One point/re-api request at a time, nominally every 30 seconds |
| Route enrichment on | ADSBDB/HexDB only for selected-aircraft route work |
| External tiles on | OpenStreetMap only for uncached/expired map tiles |

Auto and Public use the public adsb.lol endpoint. Feeder re-api is used only
when explicitly selected; upload state never changes provider authorization.

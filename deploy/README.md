# Raspberry Pi unattended deployment

The service file runs one Flask process and one owned collector thread. A fatal
collector exception terminates the process so systemd can restart the complete
application. It does not require a display attached to the Pi.

## Install or update

Copy the repository to `/opt/FlightRadar`, create its virtual environment, and
install dependencies. Adjust the service user or paths if your deployment uses
another location.

```bash
sudo useradd --system --home /opt/FlightRadar --shell /usr/sbin/nologin flightradar
sudo chown -R flightradar:flightradar /opt/FlightRadar
sudo -u flightradar /opt/FlightRadar/.venv/bin/pip install -r /opt/FlightRadar/requirements.txt
sudo install -m 0644 deploy/flightradar.service /etc/systemd/system/flightradar.service
sudo systemctl daemon-reload
sudo systemctl enable --now flightradar.service
```

Optionally set a deploy identifier in `/etc/default/flightradar`:

```text
FLIGHTRADAR_VERSION=git-commit-or-release-name
```

Bound journal storage globally in `/etc/systemd/journald.conf`, for example
`SystemMaxUse=100M`, then restart journald. The quiet adsb.lol relay discards
residual child output; relay lifecycle errors still go to the app journal.

## Validate and diagnose

```bash
systemctl status readsb.service flightradar.service
curl -fsS http://127.0.0.1:5000/api/health/live
curl -fsS http://127.0.0.1:5000/api/health/ready
curl -fsS http://127.0.0.1:5000/api/diagnostics
journalctl -u flightradar.service --since '30 minutes ago'
```

The readiness endpoint checks database access and a current collector heartbeat.
A healthy empty sky remains ready. The diagnostics response distinguishes valid
empty local data, receiver failure, remote empty data, stale rejection, rate
limiting, and authorization failure.

For an `unauthorized` result record the provider, HTTP status, response headers,
Pi outbound public IP, configured latitude/longitude/radius, provider `now` age,
and aircraft `seen`/`seen_pos` ages. Do not log tokens or credentials. Feeder
re-api authorization is IP-based and independent from the upload relay.

## Hardware acceptance and soak

1. With the SDR and readsb operating, verify green LOCAL LIVE and that fallback
   request count does not increase while eligible local aircraft are present.
2. Replay three valid empty readsb snapshots. Verify fallback starts, requests
   are at least 15 seconds apart, and valid remote data is orange.
3. Block remote access. Verify live remote aircraft expire, the UI turns red,
   request backoff increases, and local polling continues.
4. Restore two consecutive local observations. Verify local becomes primary and
   fallback-only requests stop.
5. Run at least 24 hours and record CPU/RAM, database and log sizes, request and
   recovery counts, heartbeat age, browser responsiveness, and reboot recovery.

Useful sampling commands:

```bash
systemctl show flightradar.service -p ActiveEnterTimestamp -p NRestarts -p MemoryCurrent -p CPUUsageNSec
du -h /opt/FlightRadar/aircraft.db /opt/FlightRadar/.runtime/*
curl -fsS http://127.0.0.1:5000/api/diagnostics
```

## Rollback

Stop the service, check out the previous known-good commit, reinstall requirements
if they changed, and restart. Database migrations only add columns and retain
existing settings, so the database remains readable by this release; make a copy
of `aircraft.db` before any deployment as normal rollback protection.

```bash
sudo systemctl stop flightradar.service
cp aircraft.db aircraft.db.before-rollback
git checkout PREVIOUS_KNOWN_GOOD_COMMIT
sudo -u flightradar .venv/bin/pip install -r requirements.txt
sudo systemctl start flightradar.service
```

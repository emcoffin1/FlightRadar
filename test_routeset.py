
#!/usr/bin/env python3
"""Sample local readsb output. Python standard library only."""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone


def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = map(math.radians, (lat1, lat2))
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 3958.7613 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--label', default='test')
    p.add_argument('--duration', type=float, default=900, help='Seconds (default 900)')
    p.add_argument('--lat', type=float, required=True, help='Receiver latitude')
    p.add_argument('--lon', type=float, required=True, help='Receiver longitude')
    p.add_argument('--json', type=Path, default=Path('/run/readsb/aircraft.json'))
    p.add_argument('--output', type=Path, default=Path('logs'))
    p.add_argument('--feed-adsblol', action='store_true')
    args = p.parse_args()
    if args.duration <= 0 or not -90 <= args.lat <= 90 or not -180 <= args.lon <= 180:
        p.error('Use a positive duration and valid receiver coordinates.')
    if not args.json.is_file():
        p.error(f'{args.json} missing. Install/start readsb first.')
    args.output.mkdir(parents=True, exist_ok=True)
    label = re.sub(r'[^A-Za-z0-9_-]', '_', args.label)[:80] or 'test'
    stem = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + label
    base = args.output / stem
    feeder = None
    feedlog = None
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    unique = set()
    maximum = 0.0
    start = time.monotonic()
    previous = None
    last_now = None
    next_display = 0
    fields = ['utc', 'label', 'hex', 'callsign', 'rssi_dbfs', 'distance_mi',
              'lat', 'lon', 'alt_baro_ft', 'seen_s', 'seen_pos_s', 'messages']
    try:
        if args.feed_adsblol:
            binary = shutil.which('readsb') or '/usr/local/bin/readsb'
            if not Path(binary).is_file():
                raise RuntimeError('readsb executable not found.')
            config = Path.home() / '.config' / 'adsb-test'
            config.mkdir(parents=True, exist_ok=True)
            identity = config / 'station_uuid'
            if not identity.exists():
                identity.write_text(str(uuid.uuid4()) + '\n')
            station = str(uuid.UUID(identity.read_text().strip()))
            feedlog = base.with_suffix('.feeder.log').open('w')
            command = [binary, '--net-only', '--quiet',
                       '--net-ri-port=0', '--net-ro-port=0', '--net-sbs-port=0',
                       '--net-bi-port=0', '--net-bo-port=0',
                       '--net-connector=127.0.0.1,30005,beast_in',
                       '--net-connector=feed.adsb.lol,30004,beast_reduce_plus_out,in.adsb.lol,1337,uuid=' + station]
            feeder = subprocess.Popen(command, stdout=feedlog, stderr=subprocess.STDOUT)
            print('Upload relay requested. Connection/retry details:', feedlog.name)
            print('A running relay alone does not confirm adsb.lol accepted your feed.')
        else:
            print('This script will not start an upload relay.')
        print('Aircraft samples:', base.with_suffix('.csv'))
        print('Reception summary:', base.with_suffix('.summary.csv'))
        print('Ctrl+C stops this test and its upload relay; readsb keeps receiving.')
        with base.with_suffix('.csv').open('w', newline='') as af, \
             base.with_suffix('.summary.csv').open('w', newline='') as sf:
            aircraft_csv = csv.DictWriter(af, fieldnames=fields)
            aircraft_csv.writeheader()
            summary = csv.writer(sf)
            summary.writerow(['utc', 'label', 'active_aircraft', 'unique_aircraft',
                              'decoded_messages_per_s', 'max_distance_mi'])
            while not stopped and time.monotonic() - start < args.duration:
                if feeder is not None and feeder.poll() is not None:
                    raise RuntimeError(f'Upload relay exited ({feeder.returncode}); see {feedlog.name}')
                try:
                    data = json.loads(args.json.read_text())
                    now = float(data['now'])
                    if abs(time.time() - now) > 10:
                        raise ValueError('JSON timestamp is stale or system clock is incorrect')
                    if now == last_now:
                        time.sleep(0.25)
                        continue
                    last_now = now
                    count = int(data['messages'])
                    rate = None
                    if previous and now > previous[0] and count >= previous[1]:
                        rate = (count - previous[1]) / (now - previous[0])
                    previous = (now, count)
                    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
                    active = [a for a in data.get('aircraft', []) if a.get('seen', 999) < 2]
                    for a in active:
                        unique.add(a['hex'])
                        distance = ''
                        if 'lat' in a and 'lon' in a and a.get('seen_pos', 999) < 10:
                            distance = round(distance_miles(args.lat, args.lon, a['lat'], a['lon']), 2)
                            maximum = max(maximum, distance)
                        aircraft_csv.writerow(dict(zip(fields, [stamp, args.label, a['hex'],
                            a.get('flight', '').strip(), a.get('rssi', ''), distance,
                            a.get('lat', ''), a.get('lon', ''), a.get('alt_baro', ''),
                            a.get('seen', ''), a.get('seen_pos', ''), a.get('messages', '')])))
                    summary.writerow([stamp, args.label, len(active), len(unique),
                                      '' if rate is None else round(rate, 2), maximum])
                    af.flush()
                    sf.flush()
                    if time.monotonic() >= next_display:
                        rate_text = '--' if rate is None else f'{rate:.1f}'
                        print(f'{len(active):3d} active | {len(unique):3d} unique | '
                              f'{rate_text} msg/s | max {maximum:.1f} mi')
                        for a in sorted(active, key=lambda a: a.get('rssi', -999), reverse=True)[:5]:
                            print(f"  {a['hex']:7} {a.get('flight', '').strip():8} RSSI {a.get('rssi', '--')} dBFS")
                        next_display = time.monotonic() + 5
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    print('Waiting for valid receiver data:', exc)
                    previous = None
                time.sleep(1)
    finally:
        if feeder is not None and feeder.poll() is None:
            feeder.terminate()
            try:
                feeder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                feeder.kill()
                feeder.wait()
        if feedlog:
            feedlog.close()
        print(f'Test ended: {len(unique)} unique aircraft; maximum {maximum:.1f} miles.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc))

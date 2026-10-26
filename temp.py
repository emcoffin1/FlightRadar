import atexit
from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging
from math import asin, cos, radians, sin, sqrt
from threading import Lock
from time import monotonic, sleep, time

import requests

from adsblol import ADSB_LOL
from closest_fallback import ClosestAircraftFallback
from database import (
    COLLECTOR_STATUS_FIELDS,
    connect_database,
    ensure_aircraft_metadata,
    ensure_aircraft_schema,
    ensure_mode_settings,
)
from config import (
    ADSB_LOL_POLL_SECONDS,
    AIRCRAFT_RECORD_RETENTION_SECONDS,
    DATABASE_PATH,
    HISTORY_MAX_AGE_SECONDS,
    HISTORY_MAX_POINTS,
    HISTORY_PRUNE_INTERVAL_SECONDS,
    LOCAL_RECEIVER_POLL_SECONDS,
    MAX_COLLECTION_RANGE,
    MESSAGE_FRESH_SECONDS,
    POSITION_FRESH_SECONDS,
    POSITION_CHANGE_EPSILON,
    POSITION_RECORD_MIN_SECONDS,
    RUNTIME_DIR,
    REMOTE_RETRY_MIN_SECONDS,
    REMOTE_AUTH_RETRY_SECONDS,
    REMOTE_SNAPSHOT_MAX_AGE_SECONDS,
    TRAIL_MAX_PLAUSIBLE_SPEED_KT,
    TRAIL_SEGMENT_GAP_SECONDS,
)
from local_receiver import ADSBLolFeeder, LocalReceiver
from freshness import finite_number, is_fresh
from retry_after import retry_after_seconds


LOGGER = logging.getLogger("adsb_display.collector")


def _position_source_priority(source):
    """Prefer local reception when observations have identical timestamps."""
    value = str(source or "")
    if value == "local":
        return 2
    if value.startswith("remote_"):
        return 1
    return 0


def _position_candidate(aircraft, *, now):
    latitude = finite_number(aircraft.get("lat"))
    longitude = finite_number(aircraft.get("lon"))
    distance = finite_number(aircraft.get("range"))
    observed_at = finite_number(aircraft.get("position_observed_at"))
    source = str(aircraft.get("position_source") or "").strip()
    if (
        latitude is None or not -90 <= latitude <= 90
        or longitude is None or not -180 <= longitude <= 180
        or distance is None or distance < 0
        or observed_at is None
        or not is_fresh(observed_at, POSITION_FRESH_SECONDS, now=now)
        or not source
    ):
        return None
    return {
        "lat": latitude,
        "lon": longitude,
        "range": distance,
        "position_observed_at": observed_at,
        "position_source": source,
        "alt_baro": aircraft.get("alt_baro"),
        "gs": aircraft.get("gs"),
        "track": aircraft.get("track"),
    }


def _position_wins(candidate, current):
    if candidate is None:
        return False
    current_time = finite_number(current.get("position_observed_at"))
    if current_time is None:
        return True
    incoming_time = candidate["position_observed_at"]
    if incoming_time != current_time:
        return incoming_time > current_time
    incoming_source = candidate["position_source"]
    current_source = current.get("position_source")
    return (
        _position_source_priority(incoming_source), incoming_source
    ) > (
        _position_source_priority(current_source), str(current_source or "")
    )


def _distance_nm(lat1, lon1, lat2, lon2):
    earth_radius_nm = 3440.065
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return earth_radius_nm * 2 * asin(min(1, sqrt(value)))


class RemoteAircraftPoller:
    """One-worker remote snapshot poller that never blocks local collection."""

    def __init__(self, client, *, monotonic_clock=monotonic, wall_clock=time):
        self.client = client
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remote-adsb")
        self.future = None
        self.generation = 0
        self.config = None
        self.snapshot = []
        self.last_success_at = None
        self.snapshot_generated_at = None
        self.next_due = 0
        self.status = "disabled"
        self.error = None
        self.provider = None
        self.lock = Lock()

    @staticmethod
    def select_provider(preference, local_enabled, upload_enabled):
        if preference == "feeder":
            return "feeder"
        if preference == "public":
            return "public"
        return "feeder" if local_enabled and upload_enabled else "public"

    def configure(self, *, enabled, provider, latitude, longitude, radius):
        next_config = (
            bool(enabled), provider, float(latitude), float(longitude), int(radius)
        )
        with self.lock:
            if next_config == self.config:
                return
            self.config = next_config
            self.generation += 1
            self.snapshot = []
            self.last_success_at = None
            self.snapshot_generated_at = None
            self.next_due = 0
            self.error = None
            self.provider = provider if enabled else None
            self.status = "idle" if enabled else "disabled"
            if self.future is not None:
                self.future.cancel()

    def _fetch(self, generation, provider, latitude, longitude, radius):
        data = self.client.GET_point(
            radius,
            provider=provider,
            lat=latitude,
            lon=longitude,
        )
        return generation, data

    def _finish(self, now_monotonic, now_wall):
        future = self.future
        if future is None or not future.done():
            return
        self.future = None
        try:
            generation, data = future.result()
            if generation != self.generation:
                return
            self.snapshot = data
            self.last_success_at = now_wall
            self.snapshot_generated_at = max(
                (item.get("snapshot_generated_at") or 0 for item in data),
                default=now_wall,
            )
            self.next_due = now_monotonic + ADSB_LOL_POLL_SECONDS
            self.status = "authorized" if self.provider == "feeder" else "ready"
            self.error = None
        except CancelledError:
            return
        except requests.exceptions.HTTPError as error:
            response = getattr(error, "response", None)
            status_code = response.status_code if response is not None else None
            if status_code in {401, 403}:
                delay = REMOTE_AUTH_RETRY_SECONDS
                self.status = "unauthorized"
            elif status_code == 429:
                delay = max(
                    REMOTE_RETRY_MIN_SECONDS,
                    retry_after_seconds(error, now=now_wall) or 0,
                )
                self.status = "rate_limited"
            else:
                delay = REMOTE_RETRY_MIN_SECONDS
                self.status = "error"
            self.next_due = now_monotonic + delay
            self.error = str(error)
            LOGGER.warning(
                "Remote %s poll failed; retrying in %s seconds: %s",
                self.provider, delay, error,
            )
        except (requests.exceptions.RequestException, ValueError) as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            LOGGER.warning("Remote %s poll failed: %s", self.provider, error)
        except Exception as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            LOGGER.exception("Unexpected remote %s poll failure", self.provider)

    def tick(self):
        now_monotonic = self.monotonic_clock()
        now_wall = self.wall_clock()
        with self.lock:
            self._finish(now_monotonic, now_wall)
            enabled, provider, latitude, longitude, radius = self.config or (
                False, None, 0, 0, 0
            )
            if enabled and self.future is None and now_monotonic >= self.next_due:
                generation = self.generation
                self.status = "requesting"
                self.future = self.executor.submit(
                    self._fetch,
                    generation,
                    provider,
                    latitude,
                    longitude,
                    radius,
                )

            if (
                self.last_success_at is None
                or now_wall - self.last_success_at > REMOTE_SNAPSHOT_MAX_AGE_SECONDS
            ):
                return []
            return [dict(item) for item in self.snapshot]

    def diagnostics(self):
        with self.lock:
            now_monotonic = self.monotonic_clock()
            now_wall = self.wall_clock()
            return {
                "provider": self.provider,
                "status": self.status,
                "last_success_at": self.last_success_at,
                "snapshot_generated_at": self.snapshot_generated_at,
                "snapshot_age_seconds": (
                    None if self.last_success_at is None
                    else max(0, now_wall - self.last_success_at)
                ),
                "next_retry_at": (
                    None if self.next_due <= now_monotonic
                    else now_wall + self.next_due - now_monotonic
                ),
                "error": self.error,
                "in_flight": self.future is not None,
            }

    def shutdown(self):
        with self.lock:
            self.generation += 1
            if self.future is not None:
                self.future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

class Database:
    def __init__(self, db_path=DATABASE_PATH):
        self.db_path = db_path
        self.last_history_prune = 0
        self.create_table()

    def connect(self):
        return connect_database(self.db_path)

    def execute_query(self, query: str):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            conn.commit()
            return cursor.fetchall()

    def create_table(self):
        with self.connect() as conn:
            ensure_aircraft_schema(conn, create_table=True)
            ensure_mode_settings(conn)
            metadata_count = ensure_aircraft_metadata(conn)
        if metadata_count:
            LOGGER.info("Local aircraft metadata available: %s records", metadata_count)

    def enrich_local_aircraft(self, data):
        aircraft = data.get("ac", [])
        aircraft_hexes = {
            str(item.get("hex") or "").strip().lower()
            for item in aircraft
            if item.get("hex")
        }
        if not aircraft_hexes:
            return data

        placeholders = ",".join("?" for _ in aircraft_hexes)
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT hex, registration, type_code, db_flags
                FROM aircraft_metadata
                WHERE hex IN ({placeholders})
            """, tuple(aircraft_hexes)).fetchall()
        metadata = {row[0]: row for row in rows}

        for item in aircraft:
            row = metadata.get(str(item.get("hex") or "").strip().lower())
            if row is None:
                continue
            if not item.get("r") and row[1]:
                item["r"] = row[1]
            if not item.get("t") and row[2]:
                item["t"] = row[2]
            if "dbFlags" not in item and row[3]:
                item["dbFlags"] = row[3]
        return data

    def get_collection_settings(self):
        settings = self.execute_query("""
            SELECT
                normal_enabled,
                military_enabled,
                search_range_nm,
                center_lat,
                center_lon,
                local_receiver_enabled,
                receiver_address,
                receiver_beast_port,
                adsblol_upload_enabled,
                remote_aircraft_enabled,
                fallback_aircraft_enabled,
                offline_mode_enabled,
                remote_endpoint_preference
            FROM app_settings
            WHERE id = 1
        """)[0]

        return (
            bool(settings[0]),
            bool(settings[1]),
            int(settings[2]),
            float(settings[3]),
            float(settings[4]),
            bool(settings[5]),
            str(settings[6]),
            int(settings[7]),
            bool(settings[8]),
            bool(settings[9]),
            bool(settings[10]),
            bool(settings[11]),
            str(settings[12]),
        )

    def _record_position(self, conn, aircraft_id, aircraft):
        try:
            latitude = float(aircraft['lat'])
            longitude = float(aircraft['lon'])
            recorded_at = float(aircraft['position_observed_at'])
        except (KeyError, TypeError, ValueError):
            return

        previous = conn.execute("""
            SELECT recorded_at, lat, lon, segment_id
            FROM aircraft_positions
            WHERE aircraft_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (aircraft_id,)).fetchone()

        if previous is not None:
            elapsed = recorded_at - float(previous[0])
            if elapsed <= 0:
                return
            if (
                elapsed < POSITION_RECORD_MIN_SECONDS
                or (
                abs(float(previous[1]) - latitude) < POSITION_CHANGE_EPSILON
                and abs(float(previous[2]) - longitude) < POSITION_CHANGE_EPSILON
                )
            ):
                return
            distance = _distance_nm(
                float(previous[1]), float(previous[2]), latitude, longitude
            )
            implied_speed = distance / elapsed * 3600
            segment_id = int(previous[3])
            if (
                elapsed > TRAIL_SEGMENT_GAP_SECONDS
                or implied_speed > TRAIL_MAX_PLAUSIBLE_SPEED_KT
            ):
                segment_id += 1
        else:
            segment_id = 1

        conn.execute("""
            INSERT INTO aircraft_positions (
                aircraft_id,
                recorded_at,
                lat,
                lon,
                segment_id
            ) VALUES (?, ?, ?, ?, ?)
        """, (aircraft_id, recorded_at, latitude, longitude, segment_id))

    def _prune_history(self, conn, aircraft_ids, recorded_at):
        if recorded_at - self.last_history_prune < HISTORY_PRUNE_INTERVAL_SECONDS:
            return
        self.last_history_prune = recorded_at
        conn.execute(
            "DELETE FROM aircraft_positions WHERE recorded_at < ?",
            (recorded_at - HISTORY_MAX_AGE_SECONDS,),
        )
        for aircraft_id in aircraft_ids:
            conn.execute("""
                DELETE FROM aircraft_positions
                WHERE aircraft_id = ?
                    AND id NOT IN (
                        SELECT id
                        FROM aircraft_positions
                        WHERE aircraft_id = ?
                        ORDER BY recorded_at DESC
                        LIMIT ?
                    )
            """, (aircraft_id, aircraft_id, HISTORY_MAX_POINTS))

    def _expire_aircraft(self, conn, now):
        # Keep metadata briefly for reappearance/route reuse, but live API
        # visibility is governed by the much shorter position freshness limit.
        conn.execute("""
            DELETE FROM adsb_data
            WHERE message_observed_at IS NULL
                OR message_observed_at < ?
        """, (now - AIRCRAFT_RECORD_RETENTION_SECONDS,))

    def update_collector_status(self, **values):
        if not values:
            return
        values = {
            key: value for key, value in values.items()
            if key in COLLECTOR_STATUS_FIELDS
        }
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE collector_status SET {assignments} WHERE id = 1",
                tuple(values.values()),
            )

    def update_ac(self, data, authoritative_sources=None):
        """
        Update the current aircraft snapshot and append changed positions.

        Observation timestamps determine live visibility. Metadata and bounded
        history remain available briefly for reappearance and route reuse.
        """
        recorded_at = time()

        with self.connect() as conn:
            aircraft_hexes = {
                str(item.get("hex") or "").strip().lower()
                for item in data
                if isinstance(item, dict) and item.get("hex")
            }
            current_aircraft = {}
            aircraft_hexes = tuple(aircraft_hexes)
            for offset in range(0, len(aircraft_hexes), 500):
                batch = aircraft_hexes[offset:offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(f"""
                    SELECT id, hex, position_observed_at, position_source
                    FROM adsb_data
                    WHERE hex IN ({placeholders})
                """, batch).fetchall()
                current_aircraft.update({
                    row[1]: {
                        "id": row[0],
                        "position_observed_at": row[2],
                        "position_source": row[3],
                    }
                    for row in rows
                })
            current_aircraft_ids = set()

            for ac in data:
                try:
                    current = current_aircraft.get(ac['hex'])
                    aircraft_id = current["id"] if current is not None else None
                    decoded_flags = ', '.join(ac.get('flag', []))
                    candidate = _position_candidate(ac, now=recorded_at)
                    position_update = (
                        candidate is not None
                        and (current is None or _position_wins(candidate, current))
                    )

                    if aircraft_id is not None:
                        query = """
                    UPDATE adsb_data
                    SET airline = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE airline
                        END,
                        airline_icao = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE airline_icao
                        END,
                        departure = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE departure
                        END,
                        departure_name = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE departure_name
                        END,
                        destination = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE destination
                        END,
                        destination_name = CASE
                            WHEN callsign IS NOT ? THEN NULL
                            ELSE destination_name
                        END,
                        route_lookup_complete = CASE
                            WHEN callsign IS NOT ? THEN 0
                            ELSE route_lookup_complete
                        END,
                        callsign = ?, t = ?, r = ?,
                        lat = CASE WHEN ? THEN ? ELSE lat END,
                        lon = CASE WHEN ? THEN ? ELSE lon END,
                        range = CASE WHEN ? THEN ? ELSE range END,
                        alt_baro = CASE WHEN ? THEN ? ELSE alt_baro END,
                        gs = CASE WHEN ? THEN ? ELSE gs END,
                        track = CASE WHEN ? THEN ? ELSE track END,
                        type = ?, category = ?, flag = ?, missed_updates = 0,
                        message_observed_at = MAX(
                            COALESCE(message_observed_at, 0),
                            COALESCE(?, 0)
                        ),
                        position_observed_at = CASE
                            WHEN ? THEN ? ELSE position_observed_at END,
                        snapshot_generated_at = MAX(
                            COALESCE(snapshot_generated_at, 0),
                            COALESCE(?, 0)
                        ),
                        source_fetched_at = MAX(
                            COALESCE(source_fetched_at, 0),
                            COALESCE(?, 0)
                        ),
                        collector_updated_at = ?, source = ?,
                        position_source = CASE
                            WHEN ? THEN ? ELSE position_source END
                    WHERE id = ?;
                        """
                        items = [
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['callsign'],
                            ac['t'],
                            ac['r'],
                            int(position_update),
                            candidate["lat"] if candidate else None,
                            int(position_update),
                            candidate["lon"] if candidate else None,
                            int(position_update),
                            candidate["range"] if candidate else None,
                            int(position_update),
                            candidate["alt_baro"] if candidate else None,
                            int(position_update),
                            candidate["gs"] if candidate else None,
                            int(position_update),
                            candidate["track"] if candidate else None,
                            ac['type'],
                            ac['category'],
                            decoded_flags,
                            ac.get('message_observed_at'),
                            int(position_update),
                            candidate["position_observed_at"] if candidate else None,
                            ac.get('snapshot_generated_at'),
                            ac.get('source_fetched_at'),
                            recorded_at,
                            ac.get('source'),
                            int(position_update),
                            candidate["position_source"] if candidate else None,
                            aircraft_id,
                        ]
                        conn.execute(query, items)
                    else:
                        cursor = conn.execute("""
                            INSERT INTO adsb_data (
                                hex, callsign, t, r, lat, lon, range,
                                alt_baro, track, gs, type, category, flag,
                                airline, airline_icao, departure,
                                departure_name, destination, destination_name,
                                missed_updates, message_observed_at,
                                position_observed_at, snapshot_generated_at,
                                source_fetched_at, collector_updated_at,
                                source, position_source
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?
                            )
                        """, (
                            ac['hex'],
                            ac['callsign'],
                            ac['t'],
                            ac['r'],
                            candidate["lat"] if candidate else None,
                            candidate["lon"] if candidate else None,
                            candidate["range"] if candidate else None,
                            candidate["alt_baro"] if candidate else None,
                            candidate["track"] if candidate else None,
                            candidate["gs"] if candidate else None,
                            ac['type'],
                            ac['category'],
                            decoded_flags,
                            ac['airline'],
                            ac['airline_icao'],
                            ac['departure'],
                            ac['departure_name'],
                            ac['destination'],
                            ac['destination_name'],
                            ac.get('message_observed_at'),
                            candidate["position_observed_at"] if candidate else None,
                            ac.get('snapshot_generated_at'),
                            ac.get('source_fetched_at'),
                            recorded_at,
                            ac.get('source'),
                            candidate["position_source"] if candidate else None,
                        ))
                        aircraft_id = cursor.lastrowid
                        current_aircraft[ac['hex']] = {
                            "id": aircraft_id,
                            "position_observed_at": (
                                candidate["position_observed_at"]
                                if candidate else None
                            ),
                            "position_source": (
                                candidate["position_source"]
                                if candidate else None
                            ),
                        }
                        current = current_aircraft[ac['hex']]

                    current_aircraft_ids.add(aircraft_id)
                    if position_update:
                        current["position_observed_at"] = candidate[
                            "position_observed_at"
                        ]
                        current["position_source"] = candidate["position_source"]
                    elif candidate is None:
                        stale_sources = [
                            value for value in str(ac.get("source") or "").split("+")
                            if value
                        ]
                        snapshot_time = finite_number(
                            ac.get("snapshot_generated_at")
                            or ac.get("source_fetched_at")
                        )
                        current_position_time = finite_number(
                            current.get("position_observed_at")
                        )
                        if (
                            current.get("position_source") in stale_sources
                            and snapshot_time is not None
                            and current_position_time is not None
                            and snapshot_time >= current_position_time
                        ):
                            placeholders = ", ".join("?" for _ in stale_sources)
                            conn.execute(f"""
                                UPDATE adsb_data
                                SET lat = NULL,
                                    lon = NULL,
                                    range = NULL,
                                    position_observed_at = NULL,
                                    position_source = NULL
                                WHERE id = ?
                                    AND position_source IN ({placeholders})
                            """, (aircraft_id, *stale_sources))
                            current["position_observed_at"] = None
                            current["position_source"] = None
                    if position_update:
                        self._record_position(
                            conn,
                            aircraft_id,
                            {**ac, **candidate},
                        )
                except Exception as error:
                    raise Exception(
                        f"ERROR: {error} - \nData:\n {ac}"
                    ) from error

            if authoritative_sources:
                placeholders = ", ".join("?" for _ in current_aircraft_ids)
                source_placeholders = ", ".join(
                    "?" for _ in authoritative_sources
                )
                exclusion = (
                    f"AND id NOT IN ({placeholders})"
                    if current_aircraft_ids else ""
                )
                conn.execute(f"""
                    UPDATE adsb_data
                    SET lat = NULL,
                        lon = NULL,
                        range = NULL,
                        position_observed_at = NULL,
                        position_source = NULL
                    WHERE position_source IN ({source_placeholders})
                        {exclusion}
                """, (
                    *tuple(authoritative_sources),
                    *tuple(current_aircraft_ids),
                ))

            self._expire_aircraft(conn, recorded_at)
            self._prune_history(conn, current_aircraft_ids, recorded_at)


def filter_modes(aircraft, normal_enabled, military_enabled):
    if normal_enabled and military_enabled:
        return aircraft
    if not normal_enabled and not military_enabled:
        return []
    if military_enabled:
        return [item for item in aircraft if "Military" in item.get("flag", [])]
    return [item for item in aircraft if "Military" not in item.get("flag", [])]


def merge_aircraft(remote_aircraft, local_aircraft):
    """Merge metadata while choosing the freshest valid position by time."""
    merged = {item["hex"]: item.copy() for item in remote_aircraft}
    metadata_keys = ("t", "r", "type", "category", "flag")

    for local in local_aircraft:
        existing = merged.get(local["hex"], {})
        combined = {**existing, **local}
        for key in metadata_keys:
            if local.get(key) in (None, "", "NA", ["NA"]):
                combined[key] = existing.get(key, local.get(key))
        candidates = [
            item for item in (existing, local)
            if finite_number(item.get("lat")) is not None
            and -90 <= finite_number(item.get("lat")) <= 90
            and finite_number(item.get("lon")) is not None
            and -180 <= finite_number(item.get("lon")) <= 180
            and finite_number(item.get("range")) is not None
            and finite_number(item.get("range")) >= 0
            and finite_number(item.get("position_observed_at")) is not None
            and str(item.get("position_source") or "").strip()
        ]
        candidates.sort(
            key=lambda item: (
                float(item.get("position_observed_at") or 0),
                _position_source_priority(item.get("position_source")),
                str(item.get("position_source") or ""),
            ),
            reverse=True,
        )
        if candidates:
            position = candidates[0]
            for key in (
                "lat", "lon", "range", "position_observed_at",
                "position_source", "track", "gs", "alt_baro",
            ):
                combined[key] = position.get(key)
        else:
            combined["lat"] = None
            combined["lon"] = None
            combined["range"] = None
            combined["position_observed_at"] = None
            combined["position_source"] = None
        combined["message_observed_at"] = max(
            float(existing.get("message_observed_at") or 0),
            float(local.get("message_observed_at") or 0),
        ) or None
        combined["source_fetched_at"] = max(
            float(existing.get("source_fetched_at") or 0),
            float(local.get("source_fetched_at") or 0),
        ) or None
        combined["snapshot_generated_at"] = max(
            float(existing.get("snapshot_generated_at") or 0),
            float(local.get("snapshot_generated_at") or 0),
        ) or None
        sources = {
            item.get("source") for item in (existing, local) if item.get("source")
        }
        combined["source"] = "+".join(sorted(sources)) or None
        merged[local["hex"]] = combined

    return list(merged.values())


def run_collector(
    adsb_client=None,
    *,
    max_iterations=None,
    monotonic_clock=None,
    wall_clock=None,
    sleeper=None,
    stop_event=None,
):
    """Collect local and optional remote snapshots until stopped.

    Injectable clocks and an iteration bound keep retry scheduling testable
    without real sleeps.  Production callers use the standard clocks.
    """
    adsb = adsb_client or ADSB_LOL()
    monotonic_clock = monotonic if monotonic_clock is None else monotonic_clock
    wall_clock = time if wall_clock is None else wall_clock
    sleeper = sleep if sleeper is None else sleeper
    local_receiver = LocalReceiver()
    feeder = ADSBLolFeeder(RUNTIME_DIR)
    remote_poller = RemoteAircraftPoller(
        adsb,
        monotonic_clock=monotonic_clock,
        wall_clock=wall_clock,
    )
    fallback_poller = ClosestAircraftFallback(
        adsb,
        monotonic_clock=monotonic_clock,
        wall_clock=wall_clock,
    )
    atexit.register(feeder.stop)
    atexit.register(remote_poller.shutdown)
    db = Database()
    remote_aircraft = []
    local_aircraft = []
    local_failures = 0
    iterations = 0

    while stop_event is None or not stop_event.is_set():
        collection_settings = db.get_collection_settings()
        if len(collection_settings) == 11:
            collection_settings = (*collection_settings, "auto")
        if len(collection_settings) == 12:
            collection_settings = (
                *collection_settings[:10],
                False,
                *collection_settings[10:],
            )
        (
            normal_enabled,
            military_enabled,
            display_range,
            center_lat,
            center_lon,
            local_enabled,
            receiver_address,
            receiver_beast_port,
            adsblol_upload_enabled,
            remote_aircraft_enabled,
            fallback_aircraft_enabled,
            offline_mode_enabled,
            remote_endpoint_preference,
        ) = collection_settings
        adsb.LAT = center_lat
        adsb.LON = center_lon
        adsb.RANGE = MAX_COLLECTION_RANGE
        feeder.sync(
            local_enabled and adsblol_upload_enabled and not offline_mode_enabled,
            receiver_address,
            receiver_beast_port,
        )

        if local_enabled:
            local_snapshot_succeeded = False
            local_snapshot_generated_at = None
            local_snapshot_fetched_at = None
            try:
                local_payload = local_receiver.get_aircraft(receiver_address)
                local_snapshot_generated_at = local_payload.get("now")
                local_snapshot_fetched_at = local_payload.get("source_fetched_at")
                local_payload = db.enrich_local_aircraft(local_payload)
                local_aircraft = adsb._filter_format(
                    local_payload,
                    max_range=MAX_COLLECTION_RANGE,
                    source="local",
                    fetched_at=local_payload["source_fetched_at"],
                )
                local_failures = 0
                local_snapshot_succeeded = True
            except (OSError, ValueError, requests.exceptions.RequestException) as error:
                local_failures += 1
                if local_failures == 1 or local_failures % 30 == 0:
                    LOGGER.warning(
                        "Local readsb poll failed (%s consecutive): %s",
                        local_failures,
                        error,
                    )
        else:
            local_aircraft = []
            local_failures = 0
            local_snapshot_succeeded = False
            local_snapshot_generated_at = None
            local_snapshot_fetched_at = None

        provider = RemoteAircraftPoller.select_provider(
            remote_endpoint_preference,
            local_enabled,
            adsblol_upload_enabled,
        )
        continuous_remote = remote_aircraft_enabled and not offline_mode_enabled
        fallback_enabled = (
            fallback_aircraft_enabled
            and not remote_aircraft_enabled
            and not offline_mode_enabled
        )
        acquisition_policy = (
            "continuous_remote" if continuous_remote
            else "local_with_fallback" if fallback_enabled
            else "local_only"
        )

        eligible_local = [
            item for item in filter_modes(
                local_aircraft,
                normal_enabled,
                military_enabled,
            )
            if item.get("position_source") == "local"
            and item.get("range") is not None
            and 0 <= float(item["range"]) <= display_range
            and is_fresh(
                item.get("position_observed_at"),
                POSITION_FRESH_SECONDS,
                now=wall_clock(),
            )
        ]
        fallback_poller.configure(
            enabled=fallback_enabled,
            provider=provider,
            latitude=center_lat,
            longitude=center_lon,
            radius=display_range,
            normal_enabled=normal_enabled,
            military_enabled=military_enabled,
        )
        if not fallback_enabled:
            fallback_poller.tick(active=False)
        fallback_busy = fallback_poller.diagnostics()["in_flight"]

        remote_poller.configure(
            enabled=continuous_remote and not fallback_busy,
            provider=provider,
            latitude=center_lat,
            longitude=center_lon,
            radius=MAX_COLLECTION_RANGE,
        )
        remote_snapshot = remote_poller.tick()
        remote_aircraft = remote_snapshot if continuous_remote else []
        remote_busy = remote_poller.diagnostics()["in_flight"]
        fallback_aircraft = fallback_poller.tick(
            active=not eligible_local and not remote_busy
        )
        if fallback_enabled:
            remote_aircraft = fallback_aircraft

        data = filter_modes(
            merge_aircraft(remote_aircraft, local_aircraft),
            normal_enabled,
            military_enabled,
        )
        try:
            db.update_ac(
                data,
                authoritative_sources=(
                    {"local", "remote_fallback_public", "remote_fallback_feeder"}
                    if local_snapshot_succeeded
                    else {"remote_fallback_public", "remote_fallback_feeder"}
                ),
            )
        except TypeError as error:
            # Compatibility for small test/dry-run database adapters.
            if "authoritative_sources" not in str(error):
                raise
            db.update_ac(data)
        now_wall = wall_clock()
        remote_diagnostics = remote_poller.diagnostics()
        fallback_diagnostics = fallback_poller.diagnostics()
        status_values = dict(
            heartbeat_at=now_wall,
            local_snapshot_generated_at=local_snapshot_generated_at,
            local_fetched_at=local_snapshot_fetched_at,
            local_error=None if local_failures == 0 else "receiver poll failed",
            remote_provider=remote_diagnostics["provider"],
            remote_last_success_at=remote_diagnostics["last_success_at"],
            remote_snapshot_generated_at=remote_diagnostics["snapshot_generated_at"],
            remote_next_retry_at=remote_diagnostics["next_retry_at"],
            remote_status=remote_diagnostics["status"],
            remote_error=remote_diagnostics["error"],
            acquisition_policy=acquisition_policy,
            eligible_local_position_count=len(eligible_local),
            fallback_provider=fallback_diagnostics["provider"],
            fallback_status=fallback_diagnostics["status"],
            fallback_in_flight=int(fallback_diagnostics["in_flight"]),
            fallback_last_attempt_at=fallback_diagnostics["last_attempt_at"],
            fallback_last_success_at=fallback_diagnostics["last_success_at"],
            fallback_next_request_at=fallback_diagnostics["next_request_at"],
            fallback_http_status=fallback_diagnostics["http_status"],
            fallback_error=fallback_diagnostics["error"],
            fallback_request_count=fallback_diagnostics["request_count"],
            local_message_fresh_count=sum(
                is_fresh(item.get("message_observed_at"), MESSAGE_FRESH_SECONDS, now=now_wall)
                for item in local_aircraft
            ),
            local_position_fresh_count=sum(
                is_fresh(item.get("position_observed_at"), POSITION_FRESH_SECONDS, now=now_wall)
                for item in local_aircraft
            ),
            remote_message_fresh_count=sum(
                is_fresh(item.get("message_observed_at"), MESSAGE_FRESH_SECONDS, now=now_wall)
                for item in remote_aircraft
            ),
            remote_position_fresh_count=sum(
                is_fresh(item.get("position_observed_at"), POSITION_FRESH_SECONDS, now=now_wall)
                for item in remote_aircraft
            ),
            rejected_stale_positions=sum(
                item.get("position_observed_at") is None
                for item in local_aircraft + remote_aircraft
            ),
            upload_relay_running=int(
                feeder.process is not None and feeder.process.poll() is None
            ),
        )
        if hasattr(db, "update_collector_status"):
            db.update_collector_status(**status_values)
        LOGGER.debug(
            "Updated %s aircraft (local=%s, adsb.lol=%s, range=%s nm, "
            "center=%.6f,%.6f)",
            len(data), len(local_aircraft), len(remote_aircraft), display_range,
            center_lat, center_lon,
        )
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        sleeper(LOCAL_RECEIVER_POLL_SECONDS)

    feeder.stop()
    remote_poller.shutdown()
    fallback_poller.shutdown()


if __name__ == "__main__":
    run_collector()

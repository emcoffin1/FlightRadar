import atexit
import logging
from time import monotonic, sleep, time

import requests

from adsblol import ADSB_LOL
from database import (
    connect_database,
    ensure_aircraft_metadata,
    ensure_aircraft_schema,
    ensure_mode_settings,
)
from config import (
    ADSB_LOL_POLL_SECONDS,
    DATABASE_PATH,
    LOCAL_RECEIVER_POLL_SECONDS,
    MAX_COLLECTION_RANGE,
    POSITION_CHANGE_EPSILON,
    RUNTIME_DIR,
    STALE_PASS_LIMIT,
)
from local_receiver import ADSBLolFeeder, LocalReceiver


LOGGER = logging.getLogger("adsb_display.collector")

class Database:
    def __init__(self, db_path=DATABASE_PATH):
        self.db_path = db_path
        self.create_table()

    def connect(self):
        return connect_database(self.db_path)

    def execute_query_param(self, query: str, params: tuple = ()):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.fetchall()

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

    def get_modes(self):
        settings = self.execute_query("""
            SELECT normal_enabled, military_enabled, search_range_nm
            FROM app_settings
            WHERE id = 1
        """)[0]

        return bool(settings[0]), bool(settings[1]), int(settings[2])

    def get_coordinates(self):
        coordinates = self.execute_query("""
            SELECT center_lat, center_lon
            FROM app_settings
            WHERE id = 1
        """)[0]

        return float(coordinates[0]), float(coordinates[1])

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
                adsblol_feed_enabled
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
        )

    def _record_position(self, conn, aircraft_id, aircraft, recorded_at):
        try:
            latitude = float(aircraft['lat'])
            longitude = float(aircraft['lon'])
        except (KeyError, TypeError, ValueError):
            return

        if latitude == 0 or longitude == 0:
            return

        previous = conn.execute("""
            SELECT lat, lon
            FROM aircraft_positions
            WHERE aircraft_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (aircraft_id,)).fetchone()

        if previous is not None and (
            abs(float(previous[0]) - latitude) < POSITION_CHANGE_EPSILON
            and abs(float(previous[1]) - longitude) < POSITION_CHANGE_EPSILON
        ):
            return

        conn.execute("""
            INSERT INTO aircraft_positions (
                aircraft_id,
                recorded_at,
                lat,
                lon
            ) VALUES (?, ?, ?, ?)
        """, (aircraft_id, recorded_at, latitude, longitude))

    def _age_missing_aircraft(self, conn, current_aircraft_ids):
        if current_aircraft_ids:
            placeholders = ', '.join('?' for _ in current_aircraft_ids)
            conn.execute(
                "UPDATE adsb_data "
                "SET missed_updates = missed_updates + 1 "
                f"WHERE id NOT IN ({placeholders})",
                tuple(current_aircraft_ids),
            )
        else:
            conn.execute("""
                UPDATE adsb_data
                SET missed_updates = missed_updates + 1
            """)

        # aircraft_positions rows are removed by ON DELETE CASCADE.
        conn.execute(
            "DELETE FROM adsb_data WHERE missed_updates >= ?",
            (STALE_PASS_LIMIT,),
        )

    def update_ac(self, data):
        """
        Update the current aircraft snapshot and append changed positions.

        Aircraft survive two missed API responses. On the third consecutive
        miss their snapshot and position history are deleted together.
        """
        recorded_at = time()

        with self.connect() as conn:
            current_aircraft = {
                row[1]: row[0]
                for row in conn.execute(
                    "SELECT id, hex FROM adsb_data"
                ).fetchall()
            }
            current_aircraft_ids = set()

            for ac in data:
                try:
                    aircraft_id = current_aircraft.get(ac['hex'])
                    decoded_flags = ', '.join(ac.get('flag', []))

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
                        callsign = ?, t = ?, r = ?, lat = ?, lon = ?,
                        range = ?, alt_baro = ?, gs = ?, track = ?,
                        type = ?, category = ?, flag = ?, missed_updates = 0
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
                            ac['lat'],
                            ac['lon'],
                            ac['range'],
                            ac['alt_baro'],
                            ac['gs'],
                            ac['track'],
                            ac['type'],
                            ac['category'],
                            decoded_flags,
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
                                missed_updates
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, 0
                            )
                        """, (
                            ac['hex'],
                            ac['callsign'],
                            ac['t'],
                            ac['r'],
                            ac['lat'],
                            ac['lon'],
                            ac['range'],
                            ac['alt_baro'],
                            ac['track'],
                            ac['gs'],
                            ac['type'],
                            ac['category'],
                            decoded_flags,
                            ac['airline'],
                            ac['airline_icao'],
                            ac['departure'],
                            ac['departure_name'],
                            ac['destination'],
                            ac['destination_name'],
                        ))
                        aircraft_id = cursor.lastrowid
                        current_aircraft[ac['hex']] = aircraft_id

                    current_aircraft_ids.add(aircraft_id)
                    self._record_position(
                        conn,
                        aircraft_id,
                        ac,
                        recorded_at,
                    )
                except Exception as error:
                    raise Exception(
                        f"ERROR: {error} - \nData:\n {ac}"
                    ) from error

            self._age_missing_aircraft(conn, current_aircraft_ids)


def filter_modes(aircraft, normal_enabled, military_enabled):
    if normal_enabled and military_enabled:
        return aircraft
    if not normal_enabled and not military_enabled:
        return []
    if military_enabled:
        return [item for item in aircraft if "Military" in item.get("flag", [])]
    return [item for item in aircraft if "Military" not in item.get("flag", [])]


def merge_aircraft(remote_aircraft, local_aircraft):
    """Merge snapshots by ICAO, preferring live receiver values."""
    merged = {item["hex"]: item.copy() for item in remote_aircraft}
    metadata_keys = ("t", "r", "type", "category", "flag")

    for local in local_aircraft:
        existing = merged.get(local["hex"], {})
        combined = {**existing, **local}
        for key in metadata_keys:
            if local.get(key) in (None, "", "NA", ["NA"]):
                combined[key] = existing.get(key, local.get(key))
        merged[local["hex"]] = combined

    return list(merged.values())


def run_collector():
    adsb = ADSB_LOL()
    local_receiver = LocalReceiver()
    feeder = ADSBLolFeeder(RUNTIME_DIR)
    atexit.register(feeder.stop)
    db = Database()
    remote_aircraft = []
    local_aircraft = []
    remote_due = 0
    previous_remote_settings = None
    local_failures = 0

    while True:
        (
            normal_enabled,
            military_enabled,
            display_range,
            center_lat,
            center_lon,
            local_enabled,
            receiver_address,
            receiver_beast_port,
            adsblol_enabled,
        ) = db.get_collection_settings()
        adsb.LAT = center_lat
        adsb.LON = center_lon
        adsb.RANGE = MAX_COLLECTION_RANGE
        feeder.sync(
            local_enabled and adsblol_enabled,
            receiver_address,
            receiver_beast_port,
        )

        remote_settings = (
            center_lat,
            center_lon,
        )
        if remote_settings != previous_remote_settings:
            remote_due = 0
            previous_remote_settings = remote_settings

        if local_enabled:
            try:
                local_payload = local_receiver.get_aircraft(receiver_address)
                local_payload = db.enrich_local_aircraft(local_payload)
                local_aircraft = adsb._filter_format(
                    local_payload,
                    max_range=MAX_COLLECTION_RANGE,
                )
                local_failures = 0
            except (OSError, ValueError, requests.exceptions.RequestException) as error:
                local_failures += 1
                if local_failures >= STALE_PASS_LIMIT:
                    local_aircraft = []
                if local_failures == 1 or local_failures % 30 == 0:
                    LOGGER.warning(
                        "Local readsb poll failed (%s consecutive): %s",
                        local_failures,
                        error,
                    )
        else:
            local_aircraft = []
            local_failures = 0

        now = monotonic()
        if not adsblol_enabled:
            remote_aircraft = []
            remote_due = 0
        elif now >= remote_due:
            remote_due = now + ADSB_LOL_POLL_SECONDS
            try:
                remote_aircraft = adsb.GET_point(range=MAX_COLLECTION_RANGE)
            except requests.exceptions.HTTPError as error:
                if error.response is not None and error.response.status_code == 429:
                    retry_after = int(error.response.headers.get("Retry-After", 60))
                    remote_due = now + max(retry_after, ADSB_LOL_POLL_SECONDS)
                    LOGGER.warning("Rate limited by adsb.lol for %s seconds", retry_after)
                else:
                    LOGGER.warning("adsb.lol HTTP error: %s", error)
            except requests.exceptions.RequestException as error:
                LOGGER.warning("adsb.lol request failed: %s", error)

        data = filter_modes(
            merge_aircraft(remote_aircraft, local_aircraft),
            normal_enabled,
            military_enabled,
        )
        db.update_ac(data)
        LOGGER.debug(
            "Updated %s aircraft (local=%s, adsb.lol=%s, range=%s nm, "
            "center=%.6f,%.6f)",
            len(data), len(local_aircraft), len(remote_aircraft), display_range,
            center_lat, center_lon,
        )
        sleep(LOCAL_RECEIVER_POLL_SECONDS)


if __name__ == "__main__":
    run_collector()

from time import sleep, time

import requests

from adsblol import ADSB_LOL
from database import (
    connect_database,
    ensure_aircraft_schema,
    ensure_mode_settings,
)
from config import (
    DATABASE_PATH,
    MAX_COLLECTION_RANGE,
    POSITION_CHANGE_EPSILON,
    REFRESH_TIME,
    STALE_PASS_LIMIT,
)



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
                center_lon
            FROM app_settings
            WHERE id = 1
        """)[0]

        return (
            bool(settings[0]),
            bool(settings[1]),
            int(settings[2]),
            float(settings[3]),
            float(settings[4]),
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


def run_collector():
    adsb = ADSB_LOL()
    db = Database()

    while True:
        (
            normal_enabled,
            military_enabled,
            display_range,
            center_lat,
            center_lon,
        ) = db.get_collection_settings()
        adsb.LAT = center_lat
        adsb.LON = center_lon
        adsb.RANGE = MAX_COLLECTION_RANGE

        try:
            data = adsb.GET_all(
                range=MAX_COLLECTION_RANGE,
                military=military_enabled,
                normal=normal_enabled,
            )
            db.update_ac(data)

            print(
                "Updated "
                f"(normal={'on' if normal_enabled else 'off'}, "
                f"military={'on' if military_enabled else 'off'}, "
                f"collection_range={MAX_COLLECTION_RANGE} nm, "
                f"display_range={display_range} nm, "
                f"center={center_lat:.6f},{center_lon:.6f})"
            )
            print(db.execute_query("SELECT COUNT(*) FROM adsb_data"))

        except requests.exceptions.Timeout as error:
            print(f"Aircraft API timed out: {error}")

        except requests.exceptions.HTTPError as error:
            if error.response is not None and error.response.status_code == 429:
                retry_after = int(
                    error.response.headers.get("Retry-After", 60)
                )
                print(
                    f"Rate limited by adsb.lol; "
                    f"waiting {retry_after} seconds"
                )
                sleep(retry_after)
                continue

            print(f"Aircraft API HTTP error: {error}")

        except requests.exceptions.RequestException as error:
            print(f"Aircraft API request failed: {error}")

        sleep(REFRESH_TIME)


if __name__ == "__main__":
    run_collector()

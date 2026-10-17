"""SQLite connection and schema helpers shared by the web and collector."""

import sqlite3

from config import (
    DATABASE_PATH,
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_SEARCH_RANGE,
)


def connect_database(db_path=DATABASE_PATH, *, rows=False):
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    if rows:
        connection.row_factory = sqlite3.Row
    return connection


def ensure_aircraft_schema(conn, *, create_table=False):
    if create_table:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS adsb_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hex TEXT NOT NULL,
                callsign TEXT,
                t TEXT,
                r TEXT,
                lat REAL,
                lon REAL,
                range REAL,
                alt_baro INTEGER,
                track REAL,
                gs REAL,
                type TEXT,
                category TEXT,
                flag TEXT,
                airline TEXT,
                airline_icao TEXT,
                departure TEXT,
                departure_name TEXT,
                destination TEXT,
                destination_name TEXT,
                route_lookup_complete INTEGER NOT NULL DEFAULT 0,
                missed_updates INTEGER NOT NULL DEFAULT 0
            )
        """)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(adsb_data)").fetchall()
    }
    if not columns:
        return

    if "airline_icao" not in columns:
        conn.execute("ALTER TABLE adsb_data ADD COLUMN airline_icao TEXT")

    if "route_lookup_complete" not in columns:
        conn.execute(
            "ALTER TABLE adsb_data "
            "ADD COLUMN route_lookup_complete INTEGER NOT NULL DEFAULT 0"
        )

    if "missed_updates" not in columns:
        conn.execute(
            "ALTER TABLE adsb_data "
            "ADD COLUMN missed_updates INTEGER NOT NULL DEFAULT 0"
        )

    route_names_added = False
    if "departure_name" not in columns:
        conn.execute("ALTER TABLE adsb_data ADD COLUMN departure_name TEXT")
        route_names_added = True

    if "destination_name" not in columns:
        conn.execute("ALTER TABLE adsb_data ADD COLUMN destination_name TEXT")
        route_names_added = True

    if route_names_added:
        conn.execute("UPDATE adsb_data SET route_lookup_complete = 0")

    unresolved_route = conn.execute("""
        SELECT 1
        FROM adsb_data
        WHERE route_lookup_complete = 0
            AND (
                airline IS NOT NULL
                OR airline_icao IS NOT NULL
                OR departure IS NOT NULL
                OR departure_name IS NOT NULL
                OR destination IS NOT NULL
                OR destination_name IS NOT NULL
            )
        LIMIT 1
    """).fetchone()
    if unresolved_route:
        conn.execute("""
            UPDATE adsb_data
            SET airline = NULL,
                airline_icao = NULL,
                departure = NULL,
                departure_name = NULL,
                destination = NULL,
                destination_name = NULL
            WHERE route_lookup_complete = 0
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aircraft_id INTEGER NOT NULL,
            recorded_at REAL NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            FOREIGN KEY (aircraft_id)
                REFERENCES adsb_data(id)
                ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_aircraft_positions_aircraft_time
        ON aircraft_positions (aircraft_id, recorded_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_aircraft_positions_aircraft_id
        ON aircraft_positions (aircraft_id, id DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_adsb_data_range
        ON adsb_data (range)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_adsb_data_hex_lower
        ON adsb_data (lower(hex))
    """)


def ensure_mode_settings(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            normal_enabled INTEGER NOT NULL DEFAULT 1,
            military_enabled INTEGER NOT NULL DEFAULT 0,
            center_lat REAL NOT NULL DEFAULT 33.512561467820056,
            center_lon REAL NOT NULL DEFAULT -117.11927031211901,
            search_range_nm INTEGER NOT NULL DEFAULT 25
        )
    """)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(app_settings)").fetchall()
    }
    if "center_lat" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN center_lat REAL NOT NULL DEFAULT {DEFAULT_LAT}"
        )

    if "center_lon" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN center_lon REAL NOT NULL DEFAULT {DEFAULT_LON}"
        )

    if "search_range_nm" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN search_range_nm INTEGER NOT NULL "
            f"DEFAULT {DEFAULT_SEARCH_RANGE}"
        )

    conn.execute("""
        UPDATE app_settings
        SET search_range_nm = ?
        WHERE search_range_nm NOT IN (15, 25, 50)
    """, (DEFAULT_SEARCH_RANGE,))
    conn.execute("""
        INSERT OR IGNORE INTO app_settings (
            id,
            normal_enabled,
            military_enabled,
            center_lat,
            center_lon,
            search_range_nm
        ) VALUES (1, 1, 0, ?, ?, ?)
    """, (DEFAULT_LAT, DEFAULT_LON, DEFAULT_SEARCH_RANGE))

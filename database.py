"""SQLite connection and schema helpers shared by the web and collector."""

import csv
import gzip
import sqlite3

from config import (
    DATABASE_PATH,
    AIRCRAFT_METADATA_PATH,
    DEFAULT_ADSB_LOL_FEED_ENABLED,
    DEFAULT_LAT,
    DEFAULT_LOCAL_RECEIVER_ENABLED,
    DEFAULT_LON,
    DEFAULT_RECEIVER_ADDRESS,
    DEFAULT_RECEIVER_BEAST_PORT,
    DEFAULT_SEARCH_RANGE,
)


def decode_database_flags(value):
    """Decode tar1090-db's least-significant-bit-first flag string."""
    return sum(
        1 << bit
        for bit, character in enumerate(str(value or "").strip())
        if character == "1"
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


def ensure_aircraft_metadata(conn, metadata_path=AIRCRAFT_METADATA_PATH):
    """Import the optional local tar1090/readsb aircraft database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_metadata (
            hex TEXT PRIMARY KEY,
            registration TEXT,
            type_code TEXT,
            db_flags INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            year TEXT,
            owner_operator TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_metadata_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            source_mtime_ns INTEGER NOT NULL,
            source_size INTEGER NOT NULL,
            row_count INTEGER NOT NULL
        )
    """)

    if not metadata_path.is_file():
        return 0

    source = metadata_path.stat()
    current = conn.execute("""
        SELECT source_mtime_ns, source_size, row_count
        FROM aircraft_metadata_meta
        WHERE id = 1
    """).fetchone()
    if current is not None and (
        int(current[0]) == source.st_mtime_ns
        and int(current[1]) == source.st_size
        and int(current[2]) > 0
    ):
        return int(current[2])

    conn.execute("DELETE FROM aircraft_metadata")
    count = 0
    batch = []
    with gzip.open(
        metadata_path,
        "rt",
        encoding="utf-8",
        errors="replace",
        newline="",
    ) as source_file:
        for fields in csv.reader(source_file, delimiter=";"):
            if len(fields) < 4:
                continue
            fields.extend([""] * (7 - len(fields)))
            aircraft_hex = fields[0].strip().lower()
            if not aircraft_hex:
                continue
            batch.append((
                aircraft_hex,
                fields[1].strip() or None,
                fields[2].strip().upper() or None,
                decode_database_flags(fields[3]),
                fields[4].strip() or None,
                fields[5].strip() or None,
                fields[6].strip() or None,
            ))
            if len(batch) >= 5000:
                conn.executemany("""
                    INSERT OR REPLACE INTO aircraft_metadata (
                        hex, registration, type_code, db_flags,
                        description, year, owner_operator
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, batch)
                count += len(batch)
                batch.clear()

    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO aircraft_metadata (
                hex, registration, type_code, db_flags,
                description, year, owner_operator
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, batch)
        count += len(batch)

    conn.execute("""
        INSERT INTO aircraft_metadata_meta (
            id, source_mtime_ns, source_size, row_count
        ) VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source_mtime_ns = excluded.source_mtime_ns,
            source_size = excluded.source_size,
            row_count = excluded.row_count
    """, (source.st_mtime_ns, source.st_size, count))
    return count


def ensure_mode_settings(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            normal_enabled INTEGER NOT NULL DEFAULT 1,
            military_enabled INTEGER NOT NULL DEFAULT 0,
            center_lat REAL NOT NULL DEFAULT 33.512561467820056,
            center_lon REAL NOT NULL DEFAULT -117.11927031211901,
            search_range_nm INTEGER NOT NULL DEFAULT 25,
            local_receiver_enabled INTEGER NOT NULL DEFAULT 1,
            receiver_address TEXT NOT NULL DEFAULT '/run/readsb/aircraft.json',
            receiver_beast_port INTEGER NOT NULL DEFAULT 30005,
            adsblol_feed_enabled INTEGER NOT NULL DEFAULT 0,
            local_default_applied INTEGER NOT NULL DEFAULT 1
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

    if "local_receiver_enabled" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN local_receiver_enabled INTEGER NOT NULL "
            f"DEFAULT {int(DEFAULT_LOCAL_RECEIVER_ENABLED)}"
        )

    if "receiver_address" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            "ADD COLUMN receiver_address TEXT NOT NULL DEFAULT "
            f"'{DEFAULT_RECEIVER_ADDRESS}'"
        )

    if "receiver_beast_port" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN receiver_beast_port INTEGER NOT NULL "
            f"DEFAULT {DEFAULT_RECEIVER_BEAST_PORT}"
        )

    if "adsblol_feed_enabled" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            f"ADD COLUMN adsblol_feed_enabled INTEGER NOT NULL "
            f"DEFAULT {int(DEFAULT_ADSB_LOL_FEED_ENABLED)}"
        )

    # Existing installations predate the local-first behavior. Apply the new
    # default once without overriding later choices made in the dialog.
    if "local_default_applied" not in columns:
        conn.execute(
            "ALTER TABLE app_settings "
            "ADD COLUMN local_default_applied INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute("""
        UPDATE app_settings
        SET local_receiver_enabled = 1,
            local_default_applied = 1
        WHERE local_default_applied = 0
    """)

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
            search_range_nm,
            local_receiver_enabled,
            receiver_address,
            receiver_beast_port,
            adsblol_feed_enabled,
            local_default_applied
        ) VALUES (1, 1, 0, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        DEFAULT_LAT,
        DEFAULT_LON,
        DEFAULT_SEARCH_RANGE,
        int(DEFAULT_LOCAL_RECEIVER_ENABLED),
        DEFAULT_RECEIVER_ADDRESS,
        DEFAULT_RECEIVER_BEAST_PORT,
        int(DEFAULT_ADSB_LOL_FEED_ENABLED),
    ))

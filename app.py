import csv
import json
import logging
from math import (
    atan,
    atan2,
    cos,
    degrees,
    isfinite,
    pi,
    radians,
    sin,
    sinh,
    sqrt,
)
import os
import re
from threading import Lock, Thread
import time

import requests
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)

from adsblol import ADSB_LOL
from aircraft_assets import (
    AIRCRAFT_IMAGE_ALIASES,
    AIRCRAFT_IMAGE_FILES,
    AIRCRAFT_PHOTO_BY_TYPE,
    AIRCRAFT_PHOTO_FILES,
    AIRLINE_LOGOS,
)
from database import (
    connect_database,
    ensure_aircraft_schema,
    ensure_mode_settings,
)
from config import (
    AIRPORT_DATA_PATH,
    MAX_TILE_RADIUS_NM,
    OSM_TILE_URL,
    OSM_TILE_USER_AGENT,
    REFRESH_TIME,
    SEARCH_RANGE_OPTIONS,
    TILE_CACHE_DIR,
    TILE_CACHE_MIN_SECONDS,
)
from temp import run_collector

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO").upper(),
                    format="%(asctime)s | %(levelname) -8s | %(name)s | %(message)s")
LOGGER = logging.getLogger("adsb_display")


app = Flask(__name__, template_folder="templates", static_folder="static")
adsb_client = ADSB_LOL()
collector_thread = None
airport_data_lock = Lock()


def start_aircraft_collector():
    global collector_thread

    if collector_thread is not None and collector_thread.is_alive():
        return

    collector_thread = Thread(
        target=run_collector,
        name="aircraft-collector",
        daemon=True,
    )
    collector_thread.start()
    LOGGER.info("Aircraft collector started")


def ensure_airport_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS airports (
            source_id INTEGER PRIMARY KEY,
            ident TEXT NOT NULL,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            latitude_deg REAL NOT NULL,
            longitude_deg REAL NOT NULL,
            elevation_ft INTEGER,
            iso_country TEXT,
            iso_region TEXT,
            municipality TEXT,
            scheduled_service INTEGER NOT NULL DEFAULT 0,
            icao_code TEXT,
            iata_code TEXT,
            gps_code TEXT,
            local_code TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_airports_latitude
        ON airports (latitude_deg)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_airports_longitude
        ON airports (longitude_deg)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS airport_data_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            source_mtime_ns INTEGER NOT NULL,
            row_count INTEGER NOT NULL
        )
    """)


def optional_text(value):
    value = str(value or "").strip()
    return value or None


def optional_integer(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def ensure_airport_data():
    if not AIRPORT_DATA_PATH.is_file():
        LOGGER.error("Airport dataset is missing: %s", AIRPORT_DATA_PATH)
        return False

    source_mtime_ns = AIRPORT_DATA_PATH.stat().st_mtime_ns

    with airport_data_lock:
        with connect_database() as conn:
            ensure_airport_schema(conn)
            metadata = conn.execute("""
                SELECT source_mtime_ns, row_count
                FROM airport_data_meta
                WHERE id = 1
            """).fetchone()

            if (
                metadata is not None
                and int(metadata[0]) == source_mtime_ns
                and int(metadata[1]) > 0
            ):
                return True

            airports = []

            with AIRPORT_DATA_PATH.open(
                "r",
                encoding="utf-8",
                newline="",
            ) as source:
                for row in csv.DictReader(source):
                    if row.get("type") in {"closed", "closed_airport"}:
                        continue

                    try:
                        latitude = float(row["latitude_deg"])
                        longitude = float(row["longitude_deg"])
                        source_id = int(row["id"])
                    except (KeyError, TypeError, ValueError):
                        continue

                    airports.append((
                        source_id,
                        optional_text(row.get("ident")) or str(source_id),
                        optional_text(row.get("type")) or "unknown",
                        optional_text(row.get("name")) or "Unnamed airport",
                        latitude,
                        longitude,
                        optional_integer(row.get("elevation_ft")),
                        optional_text(row.get("iso_country")),
                        optional_text(row.get("iso_region")),
                        optional_text(row.get("municipality")),
                        int(row.get("scheduled_service") == "yes"),
                        optional_text(row.get("icao_code")),
                        optional_text(row.get("iata_code")),
                        optional_text(row.get("gps_code")),
                        optional_text(row.get("local_code")),
                    ))

            conn.execute("DELETE FROM airports")
            conn.executemany("""
                INSERT INTO airports (
                    source_id,
                    ident,
                    type,
                    name,
                    latitude_deg,
                    longitude_deg,
                    elevation_ft,
                    iso_country,
                    iso_region,
                    municipality,
                    scheduled_service,
                    icao_code,
                    iata_code,
                    gps_code,
                    local_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, airports)
            conn.execute("""
                INSERT INTO airport_data_meta (
                    id,
                    source_mtime_ns,
                    row_count
                ) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_mtime_ns = excluded.source_mtime_ns,
                    row_count = excluded.row_count
            """, (source_mtime_ns, len(airports)))

        LOGGER.info("Imported %s local airport records", len(airports))
        return True


def airport_longitude_clause(center_lon, longitude_delta):
    if longitude_delta >= 180:
        return "1 = 1", ()

    minimum = center_lon - longitude_delta
    maximum = center_lon + longitude_delta

    if minimum < -180:
        return (
            "(longitude_deg >= ? OR longitude_deg <= ?)",
            (minimum + 360, maximum),
        )

    if maximum > 180:
        return (
            "(longitude_deg >= ? OR longitude_deg <= ?)",
            (minimum, maximum - 360),
        )

    return "longitude_deg BETWEEN ? AND ?", (minimum, maximum)


def is_standard_airport(airport):
    """Keep conventional airports while excluding private/unofficial strips."""
    if airport["type"] in {"large_airport", "medium_airport"}:
        return True

    if airport["type"] != "small_airport":
        return False

    if (
        airport["scheduled_service"]
        or airport["icao_code"]
        or airport["iata_code"]
    ):
        return True

    codes = (
        airport["gps_code"],
        airport["local_code"],
        airport["ident"],
    )
    return any(
        re.fullmatch(r"(?:[KPT][A-Z0-9]{3}|[A-Z]\d{2})", code.upper())
        for code in codes
        if code
    )


def airline_logo_url(airline_icao, callsign):
    code = str(airline_icao or "").strip().upper()

    if not code:
        callsign = str(callsign or "").strip().upper()
        code = "".join(
            character for character in callsign if character.isalpha()
        )[:3]

    filename = AIRLINE_LOGOS.get(code)
    return url_for("static", filename=filename) if filename else None


def aircraft_icon_url(aircraft_type):
    code = str(aircraft_type or "").strip().upper()
    code = AIRCRAFT_IMAGE_ALIASES.get(code, code).upper()
    filename = AIRCRAFT_IMAGE_FILES.get(
        code,
        AIRCRAFT_IMAGE_FILES.get("UNIDENTIFIED"),
    )

    if filename is None:
        return None

    return url_for("static", filename=f"aircraft/{filename}")


def aircraft_image_url(aircraft_type):
    code = str(aircraft_type or "").strip().upper()
    family = AIRCRAFT_PHOTO_BY_TYPE.get(code)
    filename = AIRCRAFT_PHOTO_FILES.get(family) if family else None

    if filename is None:
        return None

    return url_for("static", filename=f"aircraft_photos/{filename}")


def is_private_aircraft(aircraft):
    callsign = str(aircraft.get("callsign") or "").strip().upper()
    registration = str(aircraft.get("r") or "").strip().upper()

    if callsign and registration and callsign == registration:
        return True

    route_lookup_complete = bool(aircraft.get("route_lookup_complete"))
    airline = str(aircraft.get("airline") or "").strip().upper()
    airline_icao = str(aircraft.get("airline_icao") or "").strip()
    departure = str(aircraft.get("departure") or "").strip().upper()
    destination = str(aircraft.get("destination") or "").strip().upper()

    return (
        route_lookup_complete
        and not airline_icao
        and airline in {"", "NA", "N/A"}
        and departure in {"", "NA", "N/A"}
        and destination in {"", "NA", "N/A"}
    )


def serialize_aircraft(row):
    if row is None:
        return None

    aircraft = dict(row)
    aircraft["airline_logo"] = airline_logo_url(
        aircraft.get("airline_icao"),
        aircraft.get("callsign"),
    )
    aircraft["aircraft_icon"] = aircraft_icon_url(aircraft.get("t"))
    aircraft["aircraft_image"] = aircraft_image_url(aircraft.get("t"))
    aircraft["is_private"] = is_private_aircraft(aircraft)
    return aircraft


def get_aircraft_by_hex(aircraft_hex):
    with connect_database(rows=True) as conn:
        ensure_aircraft_schema(conn)
        aircraft = conn.execute("""
            SELECT
                hex,
                callsign,
                t,
                r,
                lat,
                lon,
                range,
                alt_baro,
                track,
                gs,
                type,
                category,
                flag,
                airline,
                airline_icao,
                departure,
                departure_name,
                destination,
                destination_name,
                route_lookup_complete
            FROM adsb_data
            WHERE lower(hex) = lower(?)
            LIMIT 1
        """, (aircraft_hex,)).fetchone()

    return aircraft


def get_aircraft_path(aircraft_hex):
    with connect_database(rows=True) as conn:
        ensure_aircraft_schema(conn)
        positions = conn.execute("""
            SELECT
                aircraft_positions.recorded_at,
                aircraft_positions.lat,
                aircraft_positions.lon
            FROM aircraft_positions
            JOIN adsb_data
                ON adsb_data.id = aircraft_positions.aircraft_id
            WHERE lower(adsb_data.hex) = lower(?)
            ORDER BY aircraft_positions.recorded_at ASC
        """, (aircraft_hex,)).fetchall()

    return [dict(position) for position in positions]


def resolve_aircraft_route(aircraft):
    if aircraft is None:
        return aircraft

    if aircraft["route_lookup_complete"] and aircraft["departure"] is not None:
        return aircraft

    try:
        route = adsb_client.GET_flight_info(
            callsign=aircraft["callsign"],
            hex=aircraft["hex"],
        )
    except requests.exceptions.RequestException as error:
        LOGGER.warning(
            "Route lookup failed for %s: %s",
            aircraft["callsign"] or aircraft["hex"],
            error,
        )
        return aircraft

    with connect_database(rows=True) as conn:
        ensure_aircraft_schema(conn)
        conn.execute("""
            UPDATE adsb_data
            SET airline = ?,
                airline_icao = ?,
                departure = ?,
                departure_name = ?,
                destination = ?,
                destination_name = ?,
                route_lookup_complete = 1
            WHERE lower(hex) = lower(?)
                AND callsign IS ?
        """, (
            route["airline"],
            route["airline_icao"],
            route["departure"],
            route["departure_name"],
            route["destination"],
            route["destination_name"],
            aircraft["hex"],
            aircraft["callsign"],
        ))

    return get_aircraft_by_hex(aircraft["hex"])


def serialize_mode_settings(row):
    return {
        "normal": bool(row["normal_enabled"]),
        "military": bool(row["military_enabled"]),
        "range_nm": int(row["search_range_nm"]),
    }


def serialize_coordinates(row):
    return {
        "lat": float(row["center_lat"]),
        "lon": float(row["center_lon"]),
    }


def parse_coordinate(data, name, minimum, maximum):
    value = data.get(name)

    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")

    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None

    if not isfinite(coordinate) or not minimum <= coordinate <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}"
        )

    return coordinate


def great_circle_distance_nm(lat_a, lon_a, lat_b, lon_b):
    earth_radius_nm = 3440.065
    lat_a = radians(lat_a)
    lat_b = radians(lat_b)
    delta_lat = lat_b - lat_a
    delta_lon = radians(lon_b - lon_a)
    haversine = (
        sin(delta_lat / 2) ** 2
        + cos(lat_a) * cos(lat_b) * sin(delta_lon / 2) ** 2
    )
    arc = 2 * atan2(sqrt(haversine), sqrt(max(0, 1 - haversine)))
    return earth_radius_nm * arc


def tile_latitude(y, zoom):
    mercator_y = pi * (1 - 2 * y / (2 ** zoom))
    return degrees(atan(sinh(mercator_y)))


def tile_intersects_cache_radius(z, x, y, center_lat, center_lon):
    tile_count = 2 ** z
    west = x / tile_count * 360 - 180
    east = (x + 1) / tile_count * 360 - 180
    north = tile_latitude(y, z)
    south = tile_latitude(y + 1, z)
    tile_lat = tile_latitude(y + 0.5, z)
    tile_lon = (west + east) / 2
    half_diagonal_nm = max(
        great_circle_distance_nm(tile_lat, tile_lon, corner_lat, corner_lon)
        for corner_lat, corner_lon in (
            (north, west),
            (north, east),
            (south, west),
            (south, east),
        )
    )
    center_distance_nm = great_circle_distance_nm(
        center_lat,
        center_lon,
        tile_lat,
        tile_lon,
    )
    return center_distance_nm <= MAX_TILE_RADIUS_NM + half_diagonal_nm


def read_tile_metadata(metadata_path):
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def tile_cache_seconds(headers):
    cache_control = headers.get("Cache-Control", "")
    match = re.search(r"(?:^|,)\s*max-age=(\d+)", cache_control)
    source_seconds = int(match.group(1)) if match else 0
    return max(TILE_CACHE_MIN_SECONDS, source_seconds)


def serve_cached_tile(tile_path, max_age, cache_status="HIT"):
    response = send_file(
        tile_path,
        mimetype="image/png",
        conditional=True,
        max_age=max(0, int(max_age)),
    )
    response.headers["X-Local-Tile-Cache"] = cache_status
    return response


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def map_tile(z, x, y):
    tile_limit = 2 ** z if 0 <= z <= 19 else 0
    if not tile_limit or not (0 <= x < tile_limit and 0 <= y < tile_limit):
        abort(404)

    with connect_database(rows=True) as conn:
        ensure_mode_settings(conn)
        center = conn.execute("""
            SELECT center_lat, center_lon
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    if not tile_intersects_cache_radius(
        z,
        x,
        y,
        float(center["center_lat"]),
        float(center["center_lon"]),
    ):
        abort(404)

    tile_path = TILE_CACHE_DIR / str(z) / str(x) / f"{y}.png"
    metadata_path = tile_path.with_suffix(".json")
    metadata = read_tile_metadata(metadata_path)
    now = time.time()

    if tile_path.is_file():
        expires_at = float(
            metadata.get(
                "expires_at",
                tile_path.stat().st_mtime + TILE_CACHE_MIN_SECONDS,
            )
        )
        if expires_at > now:
            return serve_cached_tile(tile_path, expires_at - now)

    fetch_headers = {
        "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        "User-Agent": OSM_TILE_USER_AGENT,
        "Referer": request.referrer or request.host_url,
    }
    if metadata.get("etag"):
        fetch_headers["If-None-Match"] = metadata["etag"]
    if metadata.get("last_modified"):
        fetch_headers["If-Modified-Since"] = metadata["last_modified"]

    source_url = OSM_TILE_URL.format(z=z, x=x, y=y)

    try:
        response = requests.get(source_url, headers=fetch_headers, timeout=(5, 20))

        if response.status_code == 304 and tile_path.is_file():
            cache_seconds = tile_cache_seconds(response.headers)
            metadata["expires_at"] = now + cache_seconds
            metadata_path.write_text(
                json.dumps(metadata, indent=2) + "\n",
                encoding="utf-8",
            )
            return serve_cached_tile(
                tile_path,
                cache_seconds,
                cache_status="REVALIDATED",
            )

        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise requests.exceptions.RequestException(
                f"Tile server returned {content_type or 'unknown content'}"
            )
    except requests.exceptions.RequestException as error:
        if tile_path.is_file():
            LOGGER.warning("Using stale map tile %s after: %s", tile_path, error)
            return serve_cached_tile(
                tile_path,
                TILE_CACHE_MIN_SECONDS,
                cache_status="STALE",
            )

        LOGGER.warning("Map tile request failed for %s: %s", source_url, error)
        abort(502)

    cache_seconds = tile_cache_seconds(response.headers)
    tile_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_tile = tile_path.with_suffix(".tmp")
    temporary_tile.write_bytes(response.content)
    temporary_tile.replace(tile_path)

    metadata = {
        "source": source_url,
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "expires_at": now + cache_seconds,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return serve_cached_tile(tile_path, cache_seconds, cache_status="MISS")


@app.route("/")
def hello_world():
    with connect_database(rows=True) as conn:
        ensure_mode_settings(conn)
        coordinates = conn.execute("""
            SELECT center_lat, center_lon, search_range_nm
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    return render_template(
        "index.html",
        aircraft_refresh_ms=int(REFRESH_TIME * 1000),
        map_center=[
            float(coordinates["center_lat"]),
            float(coordinates["center_lon"]),
        ],
        search_range_nm=int(coordinates["search_range_nm"]),
    )


@app.route("/api/airports")
def get_airports():
    if not ensure_airport_data():
        return jsonify({"error": "Local airport data is unavailable"}), 503

    with connect_database(rows=True) as conn:
        ensure_mode_settings(conn)
        settings = conn.execute("""
            SELECT center_lat, center_lon, search_range_nm
            FROM app_settings
            WHERE id = 1
        """).fetchone()

        center_lat = float(settings["center_lat"])
        center_lon = float(settings["center_lon"])
        radius_nm = float(settings["search_range_nm"])
        latitude_delta = radius_nm / 60
        latitude_cosine = abs(cos(radians(center_lat)))
        longitude_delta = (
            180
            if latitude_cosine < 0.001
            else min(180, radius_nm / (60 * latitude_cosine))
        )
        longitude_clause, longitude_params = airport_longitude_clause(
            center_lon,
            longitude_delta,
        )

        candidates = conn.execute(f"""
            SELECT
                source_id,
                ident,
                type,
                name,
                latitude_deg,
                longitude_deg,
                elevation_ft,
                iso_country,
                iso_region,
                municipality,
                scheduled_service,
                icao_code,
                iata_code,
                gps_code,
                local_code
            FROM airports
            WHERE latitude_deg BETWEEN ? AND ?
                AND type IN (
                    'large_airport',
                    'medium_airport',
                    'small_airport'
                )
                AND {longitude_clause}
        """, (
            max(-90, center_lat - latitude_delta),
            min(90, center_lat + latitude_delta),
            *longitude_params,
        )).fetchall()

    airports = []

    for row in candidates:
        airport = dict(row)
        if not is_standard_airport(airport):
            continue

        distance_nm = great_circle_distance_nm(
            center_lat,
            center_lon,
            float(airport["latitude_deg"]),
            float(airport["longitude_deg"]),
        )

        if distance_nm > radius_nm:
            continue

        airport["lat"] = airport.pop("latitude_deg")
        airport["lon"] = airport.pop("longitude_deg")
        airport["distance_nm"] = distance_nm
        airport["scheduled_service"] = bool(
            airport["scheduled_service"]
        )
        airport["display_code"] = (
            airport["iata_code"]
            or airport["local_code"]
            or airport["gps_code"]
            or airport["ident"]
        )
        airports.append(airport)

    type_priority = {
        "large_airport": 0,
        "medium_airport": 1,
        "small_airport": 2,
    }
    airports.sort(key=lambda airport: (
        type_priority.get(airport["type"], 9),
        airport["distance_nm"],
        airport["display_code"],
    ))
    return jsonify(airports)


@app.route("/api/aircraft")
def get_all_aircraft():
    with connect_database(rows=True) as conn:
        ensure_aircraft_schema(conn)
        ensure_mode_settings(conn)
        display_range = conn.execute("""
            SELECT search_range_nm
            FROM app_settings
            WHERE id = 1
        """).fetchone()["search_range_nm"]

        aircraft = conn.execute("""
            SELECT
                hex,
                callsign,
                t,
                alt_baro,
                range,
                lat,
                lon,
                track,
                gs
            FROM adsb_data
            WHERE range IS NOT NULL
                AND range <= ?
            ORDER BY
                CASE WHEN range IS NULL THEN 1 ELSE 0 END,
                range ASC,
                callsign ASC
        """, (display_range,)).fetchall()

    return jsonify([serialize_aircraft(row) for row in aircraft])


@app.route("/api/aircraft/<aircraft_hex>")
def get_aircraft(aircraft_hex):
    aircraft = get_aircraft_by_hex(aircraft_hex)

    if aircraft is None:
        return jsonify({"error": "Aircraft is no longer available"}), 404

    aircraft = resolve_aircraft_route(aircraft)
    serialized = serialize_aircraft(aircraft)
    serialized["path"] = get_aircraft_path(aircraft_hex)
    return jsonify(serialized)


@app.route("/api/settings/modes", methods=["GET", "PATCH"])
def mode_settings():
    with connect_database(rows=True) as conn:
        ensure_mode_settings(conn)

        if request.method == "PATCH":
            data = request.get_json(silent=True)

            if not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON object"}), 400

            for name in ("normal", "military"):
                if name in data and not isinstance(data[name], bool):
                    return jsonify({"error": f"{name} must be true or false"}), 400

            if (
                "range_nm" in data
                and data["range_nm"] not in SEARCH_RANGE_OPTIONS
            ):
                return jsonify({
                    "error": "range_nm must be 15, 25, or 50",
                }), 400

            current = conn.execute("""
                SELECT normal_enabled, military_enabled, search_range_nm
                FROM app_settings
                WHERE id = 1
            """).fetchone()

            normal_enabled = data.get(
                "normal",
                bool(current["normal_enabled"]),
            )
            military_enabled = data.get(
                "military",
                bool(current["military_enabled"]),
            )
            search_range = data.get(
                "range_nm",
                int(current["search_range_nm"]),
            )
            conn.execute("""
                UPDATE app_settings
                SET normal_enabled = ?,
                    military_enabled = ?,
                    search_range_nm = ?
                WHERE id = 1
            """, (
                int(normal_enabled),
                int(military_enabled),
                int(search_range),
            ))

        settings = conn.execute("""
            SELECT normal_enabled, military_enabled, search_range_nm
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    return jsonify(serialize_mode_settings(settings))


@app.route("/api/settings/coordinates", methods=["GET", "PATCH"])
def coordinate_settings():
    with connect_database(rows=True) as conn:
        ensure_mode_settings(conn)

        if request.method == "PATCH":
            data = request.get_json(silent=True)

            if not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON object"}), 400

            try:
                latitude = parse_coordinate(data, "lat", -90, 90)
                longitude = parse_coordinate(data, "lon", -180, 180)
            except ValueError as error:
                return jsonify({"error": str(error)}), 400

            conn.execute("""
                UPDATE app_settings
                SET center_lat = ?, center_lon = ?
                WHERE id = 1
            """, (latitude, longitude))

        coordinates = conn.execute("""
            SELECT center_lat, center_lon
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    return jsonify(serialize_coordinates(coordinates))


@app.route("/api/aircraft/closest")
def get_closest_aircraft():
    with connect_database(rows=True) as conn:
        ensure_aircraft_schema(conn)
        ensure_mode_settings(conn)
        display_range = conn.execute("""
            SELECT search_range_nm
            FROM app_settings
            WHERE id = 1
        """).fetchone()["search_range_nm"]

        aircraft = conn.execute("""
            SELECT hex
            FROM adsb_data
            WHERE lat IS NOT NULL
                AND lon IS NOT NULL
                AND lat != 0
                AND lon != 0
                AND range IS NOT NULL
                AND range <= ?
            ORDER BY range ASC
            LIMIT 1
        """, (display_range,)).fetchone()
    if aircraft is None:
        return jsonify(None)

    aircraft = resolve_aircraft_route(get_aircraft_by_hex(aircraft["hex"]))
    return jsonify(serialize_aircraft(aircraft))


if __name__ == "__main__":
    # Werkzeug's debug reloader runs this module once in a supervisor and once
    # in the server process. Start the collector only in the server process so
    # there is never a duplicate set of ADS-B API requests.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_aircraft_collector()

    app.run(debug=True)

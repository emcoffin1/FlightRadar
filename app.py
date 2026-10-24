from collections import Counter
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
    Response,
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
from local_receiver import receiver_json_location
from route_enrichment import RouteLookupCoordinator
from config import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    AIRCRAFT_DETAIL_REFRESH_SECONDS,
    AIRCRAFT_REFRESH_SECONDS,
    COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS,
    FRONTEND_SNAPSHOT_FAILURE_GRACE_SECONDS,
    AIRPORT_DATA_PATH,
    MAX_TILE_RADIUS_NM,
    POSITION_FRESH_SECONDS,
    SOURCE_SNAPSHOT_MAX_AGE_SECONDS,
    HISTORY_MAX_POINTS,
    OSM_TILE_URL,
    OSM_TILE_USER_AGENT,
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
route_coordinator = None


def lookup_aircraft_route(callsign, aircraft_hex):
    return adsb_client.GET_flight_info(
        callsign,
        aircraft_hex,
        permission_check=route_coordinator.is_external_enabled,
    )


route_coordinator = RouteLookupCoordinator(lookup_aircraft_route)
collector_thread = None
airport_data_lock = Lock()
frontend_diagnostics_lock = Lock()
frontend_diagnostics_state = {
    "reported_at": None,
    "selected_detail_failures": 0,
    "trail_revision": 0,
    "trail_segment_count": 0,
    "trail_point_count": 0,
}
external_request_counts = Counter()
TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def start_aircraft_collector():
    global collector_thread

    if collector_thread is not None and collector_thread.is_alive():
        return

    collector_thread = Thread(
        target=run_collector,
        args=(adsb_client,),
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
    is_helicopter = aircraft.get("category") == "Rotorcraft/Helicopter"
    if is_helicopter:
        type_code = str(aircraft.get("t") or "").strip().upper()
        if (
            type_code not in AIRCRAFT_IMAGE_FILES
            and type_code not in AIRCRAFT_IMAGE_ALIASES
        ):
            aircraft["aircraft_icon"] = aircraft_icon_url("R44")
    aircraft["aircraft_image"] = aircraft_image_url(aircraft.get("t"))
    if is_helicopter and aircraft["aircraft_image"] is None:
        aircraft["aircraft_image"] = aircraft_image_url("R44")
    aircraft["is_private"] = is_private_aircraft(aircraft)
    observed_at = aircraft.get("position_observed_at")
    aircraft["position_age_seconds"] = (
        None if observed_at is None
        else max(0, time.time() - float(observed_at))
    )
    aircraft["position_fresh"] = (
        observed_at is not None
        and aircraft["position_age_seconds"] <= POSITION_FRESH_SECONDS
    )
    return aircraft


def get_aircraft_by_hex(aircraft_hex):
    now = time.time()
    with connect_database(rows=True) as conn:
        status = conn.execute(
            "SELECT heartbeat_at FROM collector_status WHERE id = 1"
        ).fetchone()
        if (
            status is None
            or status["heartbeat_at"] is None
            or now - float(status["heartbeat_at"]) > COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS
        ):
            return None
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
                route_lookup_complete,
                message_observed_at,
                position_observed_at,
                snapshot_generated_at,
                source_fetched_at,
                collector_updated_at,
                source,
                position_source
            FROM adsb_data
            WHERE lower(hex) = lower(?)
                AND position_observed_at >= ?
            LIMIT 1
        """, (aircraft_hex, now - POSITION_FRESH_SECONDS)).fetchone()

    return aircraft


def get_aircraft_path(aircraft_hex):
    with connect_database(rows=True) as conn:
        latest = conn.execute("""
            SELECT aircraft_positions.segment_id,
                MAX(aircraft_positions.id) AS revision
            FROM aircraft_positions
            JOIN adsb_data ON adsb_data.id = aircraft_positions.aircraft_id
            WHERE lower(adsb_data.hex) = lower(?)
            GROUP BY aircraft_positions.segment_id
            ORDER BY aircraft_positions.segment_id DESC
            LIMIT 1
        """, (aircraft_hex,)).fetchone()
        if latest is None:
            return [], None, 0
        positions = conn.execute("""
            SELECT
                aircraft_positions.recorded_at,
                aircraft_positions.lat,
                aircraft_positions.lon,
                aircraft_positions.segment_id
            FROM aircraft_positions
            JOIN adsb_data
                ON adsb_data.id = aircraft_positions.aircraft_id
            WHERE lower(adsb_data.hex) = lower(?)
                AND aircraft_positions.segment_id = ?
            ORDER BY aircraft_positions.recorded_at DESC
            LIMIT ?
        """, (
            aircraft_hex,
            latest["segment_id"],
            HISTORY_MAX_POINTS,
        )).fetchall()

    return (
        [dict(position) for position in reversed(positions)],
        int(latest["segment_id"]),
        int(latest["revision"]),
    )


def resolve_aircraft_route(aircraft):
    if aircraft is None:
        return aircraft
    aircraft = dict(aircraft)

    if aircraft["route_lookup_complete"] and aircraft["departure"] is not None:
        aircraft["route_status"] = (
            "unavailable"
            if aircraft["departure"] in {"NA", "N/A"}
            else "ready"
        )
        return aircraft

    with connect_database(rows=True) as conn:
        settings = conn.execute("""
            SELECT route_enrichment_enabled, offline_mode_enabled
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    external_enabled = bool(settings["route_enrichment_enabled"]) and not bool(
        settings["offline_mode_enabled"]
    )
    state = route_coordinator.get_or_enqueue(
        aircraft["callsign"],
        aircraft["hex"],
        external_enabled,
    )
    if state.get("route"):
        aircraft.update(state["route"])
        aircraft["route_lookup_complete"] = 1
    aircraft["route_status"] = state["status"]
    if "retry_after" in state:
        aircraft["route_retry_after"] = state["retry_after"]
    return aircraft


def serialize_mode_settings(row):
    return {
        "normal": bool(row["normal_enabled"]),
        "military": bool(row["military_enabled"]),
        "range_nm": int(row["search_range_nm"]),
    }


def serialize_coordinates(row):
    upload_enabled = bool(row["adsblol_upload_enabled"])
    remote_enabled = bool(row["remote_aircraft_enabled"])
    return {
        "lat": float(row["center_lat"]),
        "lon": float(row["center_lon"]),
        "local_receiver_enabled": bool(row["local_receiver_enabled"]),
        "receiver_address": row["receiver_address"],
        "receiver_beast_port": int(row["receiver_beast_port"]),
        "adsblol_upload_enabled": upload_enabled,
        "remote_aircraft_enabled": remote_enabled,
        "remote_endpoint_preference": row["remote_endpoint_preference"],
        "route_enrichment_enabled": bool(row["route_enrichment_enabled"]),
        "external_tiles_enabled": bool(row["external_tiles_enabled"]),
        "offline_mode_enabled": bool(row["offline_mode_enabled"]),
        "constrained_profile_enabled": bool(
            row["constrained_profile_enabled"]
        ),
        # Compatibility for the former combined switch.
        "adsblol_enabled": upload_enabled and remote_enabled,
        "adsblol_feed_enabled": upload_enabled,
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
        center = conn.execute("""
            SELECT center_lat,
                center_lon,
                external_tiles_enabled,
                offline_mode_enabled
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

    external_tiles_enabled = bool(center["external_tiles_enabled"]) and not bool(
        center["offline_mode_enabled"]
    )
    if not external_tiles_enabled:
        if tile_path.is_file():
            return serve_cached_tile(
                tile_path,
                TILE_CACHE_MIN_SECONDS,
                cache_status="OFFLINE-STALE",
            )
        response = Response(TRANSPARENT_PNG, mimetype="image/png")
        response.headers["Cache-Control"] = "public, max-age=300"
        response.headers["X-Local-Tile-Cache"] = "OFFLINE-MISSING"
        return response

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
        external_request_counts[f"openstreetmap:{response.status_code}"] += 1

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
        external_request_counts["openstreetmap:exception"] += 1
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
        coordinates = conn.execute("""
            SELECT center_lat,
                center_lon,
                search_range_nm,
                constrained_profile_enabled
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    return render_template(
        "index.html",
        aircraft_refresh_ms=int(AIRCRAFT_REFRESH_SECONDS * 1000),
        aircraft_detail_refresh_ms=int(
            AIRCRAFT_DETAIL_REFRESH_SECONDS * 1000
        ),
        position_fresh_ms=int(POSITION_FRESH_SECONDS * 1000),
        snapshot_failure_grace_ms=int(
            FRONTEND_SNAPSHOT_FAILURE_GRACE_SECONDS * 1000
        ),
        map_center=[
            float(coordinates["center_lat"]),
            float(coordinates["center_lon"]),
        ],
        search_range_nm=int(coordinates["search_range_nm"]),
        constrained_profile_enabled=bool(
            coordinates["constrained_profile_enabled"]
        ),
        frontend_diagnostics_url=url_for("frontend_diagnostics"),
    )


@app.route("/api/airports")
def get_airports():
    if not ensure_airport_data():
        return jsonify({"error": "Local airport data is unavailable"}), 503

    with connect_database(rows=True) as conn:
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
    now = time.time()
    with connect_database(rows=True) as conn:
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
                gs,
                category,
                position_observed_at,
                message_observed_at,
                source,
                position_source
            FROM adsb_data
            WHERE range IS NOT NULL
                AND range <= ?
                AND position_observed_at >= ?
            ORDER BY
                CASE WHEN range IS NULL THEN 1 ELSE 0 END,
                range ASC,
                callsign ASC
        """, (display_range, now - POSITION_FRESH_SECONDS)).fetchall()

        status = conn.execute("""
            SELECT heartbeat_at FROM collector_status WHERE id = 1
        """).fetchone()

    collector_fresh = bool(
        status and status["heartbeat_at"]
        and now - float(status["heartbeat_at"]) <= COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS
    )
    response = jsonify(
        [serialize_aircraft(row) for row in aircraft] if collector_fresh else []
    )
    response.headers["X-Collector-Fresh"] = "1" if collector_fresh else "0"
    response.headers["X-Collector-Heartbeat"] = (
        str(status["heartbeat_at"]) if status and status["heartbeat_at"] else ""
    )
    response.headers["X-Response-Generated-At"] = str(now)
    return response


@app.route("/basic")
def basic_display():
    return render_template(
        "basic.html",
        position_fresh_ms=int(POSITION_FRESH_SECONDS * 1000),
        heartbeat_fresh_ms=int(COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS * 1000),
        clock_skew_ms=int(CLOCK_SKEW_TOLERANCE_SECONDS * 1000),
    )


@app.route("/api/aircraft/nearest")
def get_nearest_aircraft():
    """Text-only snapshot; never schedules routes, photos, trails or API work."""
    now = time.time()
    with connect_database(rows=True) as conn:
        status = conn.execute(
            "SELECT heartbeat_at FROM collector_status WHERE id = 1"
        ).fetchone()
        heartbeat = status["heartbeat_at"] if status else None
        fresh = heartbeat is not None and (
            -CLOCK_SKEW_TOLERANCE_SECONDS <= now - float(heartbeat)
            <= COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS
        )
        row = None
        if fresh:
            row = conn.execute("""
                SELECT hex, callsign, t, r, alt_baro, range, gs, track,
                    position_observed_at, source
                FROM adsb_data
                WHERE range IS NOT NULL AND range >= 0
                    AND range <= (SELECT search_range_nm FROM app_settings WHERE id = 1)
                    AND lat IS NOT NULL AND lon IS NOT NULL
                    AND position_observed_at BETWEEN ? AND ?
                ORDER BY range ASC, hex ASC
                LIMIT 1
            """, (now - POSITION_FRESH_SECONDS,
                  now + CLOCK_SKEW_TOLERANCE_SECONDS)).fetchone()
    response = jsonify({
        "aircraft": dict(row) if row else None,
        "collector_fresh": fresh,
        "heartbeat_at": heartbeat,
        "generated_at": now,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/aircraft/<aircraft_hex>")
def get_aircraft(aircraft_hex):
    aircraft = get_aircraft_by_hex(aircraft_hex)

    if aircraft is None:
        return jsonify({"error": "Aircraft is no longer available"}), 404

    aircraft = resolve_aircraft_route(aircraft)
    serialized = serialize_aircraft(aircraft)
    path, segment_id, revision = get_aircraft_path(aircraft_hex)
    serialized["path"] = path
    serialized["trail_segment_id"] = segment_id
    serialized["trail_revision"] = revision
    return jsonify(serialized)


@app.route("/api/settings/modes", methods=["GET", "PATCH"])
def mode_settings():
    with connect_database(rows=True) as conn:
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
        if request.method == "PATCH":
            data = request.get_json(silent=True)

            if not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON object"}), 400

            current = conn.execute("""
                SELECT center_lat,
                    center_lon,
                    local_receiver_enabled,
                    receiver_address,
                    receiver_beast_port,
                    adsblol_feed_enabled,
                    adsblol_upload_enabled,
                    remote_aircraft_enabled,
                    route_enrichment_enabled,
                    external_tiles_enabled,
                    offline_mode_enabled,
                    constrained_profile_enabled,
                    remote_endpoint_preference
                FROM app_settings
                WHERE id = 1
            """).fetchone()

            try:
                latitude = (
                    parse_coordinate(data, "lat", -90, 90)
                    if "lat" in data
                    else float(current["center_lat"])
                )
                longitude = (
                    parse_coordinate(data, "lon", -180, 180)
                    if "lon" in data
                    else float(current["center_lon"])
                )
            except ValueError as error:
                return jsonify({"error": str(error)}), 400

            for name in (
                "local_receiver_enabled",
                "adsblol_enabled",
                "adsblol_feed_enabled",
                "adsblol_upload_enabled",
                "remote_aircraft_enabled",
                "route_enrichment_enabled",
                "external_tiles_enabled",
                "offline_mode_enabled",
                "constrained_profile_enabled",
            ):
                if name in data and not isinstance(data[name], bool):
                    return jsonify({"error": f"{name} must be true or false"}), 400

            endpoint_preference = data.get(
                "remote_endpoint_preference",
                current["remote_endpoint_preference"],
            )
            if endpoint_preference not in {"auto", "public", "feeder"}:
                return jsonify({
                    "error": "remote_endpoint_preference must be auto, public, or feeder"
                }), 400

            receiver_address = str(data.get(
                "receiver_address",
                current["receiver_address"],
            )).strip()
            if not receiver_address or len(receiver_address) > 2048:
                return jsonify({
                    "error": "receiver_address must not be empty",
                }), 400
            if "\n" in receiver_address or "\r" in receiver_address:
                return jsonify({"error": "receiver_address is invalid"}), 400
            try:
                receiver_json_location(receiver_address)
            except ValueError as error:
                return jsonify({"error": str(error)}), 400

            beast_port = data.get(
                "receiver_beast_port",
                current["receiver_beast_port"],
            )
            if isinstance(beast_port, bool):
                beast_port = None
            try:
                beast_port = int(beast_port)
            except (TypeError, ValueError):
                beast_port = None
            if beast_port is None or not 1 <= beast_port <= 65535:
                return jsonify({
                    "error": "receiver_beast_port must be between 1 and 65535",
                }), 400

            legacy_combined = data.get(
                "adsblol_enabled",
                data.get("adsblol_feed_enabled"),
            )
            upload_enabled = data.get(
                "adsblol_upload_enabled",
                legacy_combined if legacy_combined is not None else bool(
                    current["adsblol_upload_enabled"]
                ),
            )
            remote_enabled = data.get(
                "remote_aircraft_enabled",
                legacy_combined if legacy_combined is not None else bool(
                    current["remote_aircraft_enabled"]
                ),
            )

            conn.execute("""
                UPDATE app_settings
                SET center_lat = ?,
                    center_lon = ?,
                    local_receiver_enabled = ?,
                    receiver_address = ?,
                    receiver_beast_port = ?,
                    adsblol_feed_enabled = ?,
                    adsblol_upload_enabled = ?,
                    remote_aircraft_enabled = ?,
                    route_enrichment_enabled = ?,
                    external_tiles_enabled = ?,
                    offline_mode_enabled = ?,
                    constrained_profile_enabled = ?,
                    remote_endpoint_preference = ?
                WHERE id = 1
            """, (
                latitude,
                longitude,
                int(data.get(
                    "local_receiver_enabled",
                    bool(current["local_receiver_enabled"]),
                )),
                receiver_address,
                beast_port,
                int(upload_enabled),
                int(upload_enabled),
                int(remote_enabled),
                int(data.get(
                    "route_enrichment_enabled",
                    bool(current["route_enrichment_enabled"]),
                )),
                int(data.get(
                    "external_tiles_enabled",
                    bool(current["external_tiles_enabled"]),
                )),
                int(data.get(
                    "offline_mode_enabled",
                    bool(current["offline_mode_enabled"]),
                )),
                int(data.get(
                    "constrained_profile_enabled",
                    bool(current["constrained_profile_enabled"]),
                )),
                endpoint_preference,
            ))

        coordinates = conn.execute("""
            SELECT center_lat,
                center_lon,
                local_receiver_enabled,
                receiver_address,
                receiver_beast_port,
                adsblol_feed_enabled,
                adsblol_upload_enabled,
                remote_aircraft_enabled,
                route_enrichment_enabled,
                external_tiles_enabled,
                offline_mode_enabled,
                constrained_profile_enabled,
                remote_endpoint_preference
            FROM app_settings
            WHERE id = 1
        """).fetchone()

    route_coordinator.set_external_enabled(
        bool(coordinates["route_enrichment_enabled"])
        and not bool(coordinates["offline_mode_enabled"])
    )
    return jsonify(serialize_coordinates(coordinates))


@app.route("/api/aircraft/closest")
def get_closest_aircraft():
    now = time.time()
    with connect_database(rows=True) as conn:
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
                AND position_observed_at >= ?
            ORDER BY range ASC
            LIMIT 1
        """, (display_range, now - POSITION_FRESH_SECONDS)).fetchone()
    if aircraft is None:
        return jsonify(None)

    aircraft = resolve_aircraft_route(get_aircraft_by_hex(aircraft["hex"]))
    return jsonify(serialize_aircraft(aircraft))


@app.route("/api/diagnostics")
def diagnostics():
    now = time.time()
    with connect_database(rows=True) as conn:
        counts = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM adsb_data) AS aircraft,
                (SELECT COUNT(*) FROM aircraft_positions) AS trail_points,
                (SELECT COUNT(*) FROM route_cache) AS route_cache_entries
        """).fetchone()
        collector = conn.execute("""
            SELECT * FROM collector_status WHERE id = 1
        """).fetchone()
        freshness = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN position_observed_at >= ? THEN 1 ELSE 0 END), 0) AS live,
                COALESCE(SUM(CASE WHEN position_observed_at < ? THEN 1 ELSE 0 END), 0) AS stale,
                COALESCE(SUM(CASE WHEN position_observed_at IS NULL THEN 1 ELSE 0 END), 0) AS historical
            FROM adsb_data
        """, (
            now - POSITION_FRESH_SECONDS,
            now - POSITION_FRESH_SECONDS,
        )).fetchone()
        trail = conn.execute("""
            SELECT MAX(id) AS revision,
                COUNT(DISTINCT segment_id) AS segment_count,
                COUNT(*) AS point_count
            FROM aircraft_positions
        """).fetchone()
    collector_data = dict(collector) if collector else {}
    heartbeat = collector_data.get("heartbeat_at")
    collector_data["heartbeat_age_seconds"] = (
        None if heartbeat is None else max(0, now - float(heartbeat))
    )
    collector_data["running"] = bool(
        heartbeat is not None
        and collector_data["heartbeat_age_seconds"] <= COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS
    )
    remote_success = collector_data.get("remote_last_success_at")
    collector_data["remote_snapshot_age_seconds"] = (
        None if remote_success is None else max(0, now - float(remote_success))
    )
    local_fetched = collector_data.get("local_fetched_at")
    collector_data["local_snapshot_age_seconds"] = (
        None if local_fetched is None else max(0, now - float(local_fetched))
    )
    collector_data["receiver_fresh"] = bool(
        local_fetched is not None
        and collector_data["local_snapshot_age_seconds"]
        <= SOURCE_SNAPSHOT_MAX_AGE_SECONDS
    )
    with frontend_diagnostics_lock:
        frontend_data = dict(frontend_diagnostics_state)
    return jsonify({
        "database": dict(counts),
        "freshness": dict(freshness),
        "trail": dict(trail),
        "collector": collector_data,
        "frontend": frontend_data,
        "external_requests": {
            **adsb_client.request_diagnostics(),
            **dict(external_request_counts),
        },
        "route_lookup": route_coordinator.diagnostics(),
    })


@app.route("/api/diagnostics/frontend", methods=["POST"])
def frontend_diagnostics():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    try:
        selected_failures = max(0, int(data.get("selected_detail_failures", 0)))
        trail_revision = max(0, int(data.get("trail_revision", 0)))
        trail_segments = max(0, int(data.get("trail_segment_count", 0)))
        trail_points = max(0, int(data.get("trail_point_count", 0)))
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Diagnostic counts must be integers"}), 400
    with frontend_diagnostics_lock:
        frontend_diagnostics_state.update({
            "reported_at": time.time(),
            "selected_detail_failures": selected_failures,
            "trail_revision": trail_revision,
            "trail_segment_count": trail_segments,
            "trail_point_count": trail_points,
        })
    return jsonify({"ok": True})


def initialize_application_database():
    with connect_database() as conn:
        ensure_aircraft_schema(conn, create_table=True)
        ensure_mode_settings(conn)


initialize_application_database()
with connect_database(rows=True) as conn:
    initial_network_settings = conn.execute("""
        SELECT route_enrichment_enabled, offline_mode_enabled
        FROM app_settings
        WHERE id = 1
    """).fetchone()
route_coordinator.set_external_enabled(
    bool(initial_network_settings["route_enrichment_enabled"])
    and not bool(initial_network_settings["offline_mode_enabled"])
)


if __name__ == "__main__":
    # Werkzeug's debug reloader runs this module once in a supervisor and once
    # in the server process. Start the collector only in the server process so
    # there is never a duplicate set of ADS-B API requests.
    # if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    #     start_aircraft_collector()
    start_aircraft_collector()

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    # app.run(debug=True)

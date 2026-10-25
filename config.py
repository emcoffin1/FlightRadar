"""Shared application configuration and filesystem locations."""

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "aircraft.db"

AIRCRAFT_IMAGE_DIR = APP_DIR / "static" / "aircraft"
AIRCRAFT_PHOTO_DIR = APP_DIR / "static" / "aircraft_photos"
TILE_CACHE_DIR = APP_DIR / "static" / "tiles"
AIRPORT_DATA_PATH = APP_DIR / "data" / "ourairports-airports.csv"
AIRCRAFT_METADATA_PATH = APP_DIR / "data" / "aircraft.csv.gz"
RUNTIME_DIR = APP_DIR / ".runtime"

# Local receiver data changes once per second.  Keep the browser in step with
# it, while rate-limiting the selected adsb.lol endpoint independently.
AIRCRAFT_REFRESH_SECONDS = 1
LOCAL_RECEIVER_POLL_SECONDS = 1
ADSB_LOL_POLL_SECONDS = 10
# The shared collector uses fallback acquisition only when no eligible fresh
# local position exists. Keep it independent from continuous remote polling.
ADSB_LOL_CLOSEST_POLL_SECONDS = 15
# Invalid or expired Retry-After guidance must never create a tight loop.
REMOTE_RETRY_MIN_SECONDS = 60
# Selected-aircraft trails/details are intentionally slower than snapshots.
AIRCRAFT_DETAIL_REFRESH_SECONDS = 7
# All persisted times below are UTC Unix seconds. Scheduling still uses a
# monotonic clock. These centralized limits define "live" consistently.
CLOCK_SKEW_TOLERANCE_SECONDS = 5
SOURCE_SNAPSHOT_MAX_AGE_SECONDS = 15
MESSAGE_FRESH_SECONDS = 20
POSITION_FRESH_SECONDS = 20
AIRCRAFT_RECORD_RETENTION_SECONDS = 5 * 60
COLLECTOR_HEARTBEAT_MAX_AGE_SECONDS = 10
REMOTE_SNAPSHOT_MAX_AGE_SECONDS = 30
FRONTEND_SNAPSHOT_FAILURE_GRACE_SECONDS = 15
TRAIL_SEGMENT_GAP_SECONDS = 120
TRAIL_MAX_PLAUSIBLE_SPEED_KT = 1500
REMOTE_AUTH_RETRY_SECONDS = 5 * 60
REMOTE_REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_LAT = 33.512561467820056
DEFAULT_LON = -117.11927031211901
DEFAULT_SEARCH_RANGE = 25
DEFAULT_LOCAL_RECEIVER_ENABLED = True
DEFAULT_RECEIVER_ADDRESS = "/run/readsb/aircraft.json"
DEFAULT_RECEIVER_BEAST_PORT = 30005
DEFAULT_ADSB_LOL_FEED_ENABLED = False
DEFAULT_REMOTE_AIRCRAFT_ENABLED = False
DEFAULT_FALLBACK_AIRCRAFT_ENABLED = False
DEFAULT_REMOTE_ENDPOINT_PREFERENCE = "auto"
DEFAULT_ROUTE_ENRICHMENT_ENABLED = False
DEFAULT_EXTERNAL_TILES_ENABLED = False
DEFAULT_OFFLINE_MODE_ENABLED = False
DEFAULT_CONSTRAINED_PROFILE_ENABLED = True
SEARCH_RANGE_OPTIONS = {15, 25, 50}
MAX_COLLECTION_RANGE = 50
POSITION_CHANGE_EPSILON = 0.00001
POSITION_RECORD_MIN_SECONDS = 5
HISTORY_MAX_AGE_SECONDS = 60 * 60
HISTORY_MAX_POINTS = 360
HISTORY_PRUNE_INTERVAL_SECONDS = 60

ROUTE_LOOKUP_WORKERS = 2
ROUTE_LOOKUP_MAX_PENDING = 24
ROUTE_RETRY_MIN_SECONDS = 60
ROUTE_RETRY_MAX_SECONDS = 60 * 60

TILE_RADIUS_BUFFER_NM = 10
MAX_TILE_RADIUS_NM = max(SEARCH_RANGE_OPTIONS) + TILE_RADIUS_BUFFER_NM
TILE_CACHE_MIN_SECONDS = 7 * 24 * 60 * 60
OSM_TILE_URL = os.environ.get(
    "OSM_TILE_URL",
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
)
OSM_TILE_USER_AGENT = os.environ.get(
    "OSM_TILE_USER_AGENT",
    "ADSB-Display/1.0 (local aircraft map)",
)

"""Shared application configuration and filesystem locations."""

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "aircraft.db"

AIRCRAFT_IMAGE_DIR = APP_DIR / "static" / "aircraft"
AIRCRAFT_PHOTO_DIR = APP_DIR / "static" / "aircraft_photos"
TILE_CACHE_DIR = APP_DIR / "static" / "tiles"
AIRPORT_DATA_PATH = APP_DIR / "data" / "ourairports-airports.csv"

REFRESH_TIME = 15
DEFAULT_LAT = 33.512561467820056
DEFAULT_LON = -117.11927031211901
DEFAULT_SEARCH_RANGE = 25
SEARCH_RANGE_OPTIONS = {15, 25, 50}
MAX_COLLECTION_RANGE = 50
STALE_PASS_LIMIT = 3
POSITION_CHANGE_EPSILON = 0.00001

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

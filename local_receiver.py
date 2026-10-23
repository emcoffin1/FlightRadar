"""Read local readsb JSON and optionally relay Beast data to adsb.lol."""

import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlparse
import uuid

import requests

from config import (
    MESSAGE_FRESH_SECONDS,
    POSITION_FRESH_SECONDS,
    SOURCE_SNAPSHOT_MAX_AGE_SECONDS,
)
from freshness import is_fresh, observation_time, valid_absolute_time


LOGGER = logging.getLogger("adsb_display.receiver")
DEFAULT_REMOTE_JSON_PATH = "/readsb/aircraft.json"


def receiver_json_location(address):
    """Resolve a path, full URL, or receiver hostname to aircraft.json."""
    address = str(address or "").strip()
    if not address or "\n" in address or "\r" in address:
        raise ValueError("receiver_address must not be empty")

    if address.startswith("/"):
        return Path(address)

    if "://" in address:
        parsed = urlparse(address)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "receiver_address must be a path, hostname, or HTTP URL"
            )
        return address

    return f"http://{address.rstrip('/')}{DEFAULT_REMOTE_JSON_PATH}"


def receiver_beast_host(address):
    """Return the host providing readsb's Beast TCP output."""
    address = str(address or "").strip()
    if address.startswith("/"):
        return "127.0.0.1"
    parsed = urlparse(address if "://" in address else f"//{address}")
    if not parsed.hostname:
        raise ValueError("Could not determine receiver host")
    return parsed.hostname


class LocalReceiver:
    def __init__(self, timeout=3):
        self.timeout = timeout

    def get_aircraft(self, address):
        location = receiver_json_location(address)
        if isinstance(location, Path):
            payload = json.loads(location.read_text(encoding="utf-8"))
        else:
            response = requests.get(location, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        fetched_at = time.time()

        generated_at = valid_absolute_time(payload.get("now"), now=fetched_at)
        if generated_at is None:
            raise ValueError("readsb aircraft.json has an invalid timestamp")
        if fetched_at - generated_at > SOURCE_SNAPSHOT_MAX_AGE_SECONDS:
            raise ValueError("readsb aircraft.json is stale")

        aircraft = payload.get("aircraft")
        if not isinstance(aircraft, list):
            raise ValueError("readsb JSON does not contain an aircraft list")

        normalized = []
        for raw in aircraft:
            if not isinstance(raw, dict):
                continue
            message_observed_at = observation_time(
                generated_at,
                raw.get("seen"),
                fetched_at=fetched_at,
            )
            if not is_fresh(
                message_observed_at,
                MESSAGE_FRESH_SECONDS,
                now=fetched_at,
            ):
                continue

            item = dict(raw)
            item["_message_observed_at"] = message_observed_at
            item["_snapshot_generated_at"] = generated_at
            item["_source_fetched_at"] = fetched_at
            item["_source"] = "local"

            position_observed_at = observation_time(
                generated_at,
                item.get("seen_pos"),
                fetched_at=fetched_at,
            )
            has_precise_position = "lat" in item and "lon" in item
            if not has_precise_position or not is_fresh(
                position_observed_at,
                POSITION_FRESH_SECONDS,
                now=fetched_at,
            ):
                # lastPosition and rr_lat/rr_lon remain metadata only. They
                # are never promoted into the live-position fields.
                item.pop("lat", None)
                item.pop("lon", None)
                position_observed_at = None
            item["_position_observed_at"] = position_observed_at
            normalized.append(item)

        return {
            "ac": normalized,
            "now": generated_at,
            "source_fetched_at": fetched_at,
            "source": "local",
        }


class ADSBLolFeeder:
    """Manage a readsb net-only process that relays Beast data to adsb.lol."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.process = None
        self.source = None
        self.log_handle = None
        self.last_start_attempt = 0

    def _station_uuid(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        identity_path = self.state_dir / "adsblol_station_uuid"
        try:
            return str(uuid.UUID(identity_path.read_text().strip()))
        except (FileNotFoundError, ValueError):
            station_uuid = str(uuid.uuid4())
            identity_path.write_text(station_uuid + "\n", encoding="utf-8")
            return station_uuid

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None
        self.source = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def sync(self, enabled, address, beast_port):
        if not enabled:
            self.stop()
            return

        source = (receiver_beast_host(address), int(beast_port))
        if self.process is not None and self.process.poll() is None:
            if self.source == source:
                return
            self.stop()
        elif self.process is not None:
            LOGGER.warning("adsb.lol feed relay exited; retrying")
            self.stop()

        now = time.monotonic()
        if now - self.last_start_attempt < 30:
            return
        self.last_start_attempt = now

        binary = shutil.which("readsb")
        if not binary:
            LOGGER.error(
                "adsb.lol feeding is enabled, but readsb is not installed "
                "on the radar backend"
            )
            return

        station_uuid = self._station_uuid()
        self.log_handle = (self.state_dir / "adsblol-feeder.log").open(
            "a", encoding="utf-8"
        )
        command = [
            binary,
            "--net-only",
            "--quiet",
            "--net-ri-port=0",
            "--net-ro-port=0",
            "--net-sbs-port=0",
            "--net-bi-port=0",
            "--net-bo-port=0",
            f"--net-connector={source[0]},{source[1]},beast_in",
            "--net-connector=feed.adsb.lol,30004,beast_reduce_plus_out,"
            f"in.adsb.lol,1337,uuid={station_uuid}",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            LOGGER.error("Could not start adsb.lol feed relay: %s", error)
            self.log_handle.close()
            self.log_handle = None
            return
        self.source = source
        LOGGER.info(
            "Started adsb.lol feed relay from %s:%s (pid %s)",
            source[0], source[1], self.process.pid,
        )

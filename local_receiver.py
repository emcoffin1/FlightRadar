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

        generated_at = float(payload.get("now", 0))
        if generated_at and abs(time.time() - generated_at) > 15:
            raise ValueError("readsb aircraft.json is stale")

        aircraft = payload.get("aircraft")
        if not isinstance(aircraft, list):
            raise ValueError("readsb JSON does not contain an aircraft list")

        # Ignore contacts readsb has not heard recently. Position age is
        # handled separately by the formatter, which omits missing positions.
        return {
            "ac": [
                item for item in aircraft
                if isinstance(item, dict) and float(item.get("seen", 999)) < 15
            ]
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

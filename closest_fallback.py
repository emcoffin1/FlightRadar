"""Single-worker remote fallback acquisition shared by every display."""

import atexit
from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging
from threading import Lock
from time import monotonic, time

import requests

from config import (
    ADSB_LOL_CLOSEST_POLL_SECONDS,
    POSITION_FRESH_SECONDS,
    REMOTE_AUTH_RETRY_SECONDS,
    REMOTE_RETRY_MIN_SECONDS,
)
from freshness import is_fresh
from retry_after import retry_after_seconds


LOGGER = logging.getLogger("adsb_display.closest_fallback")


def _mode_eligible(item, normal_enabled, military_enabled):
    military = "Military" in item.get("flag", [])
    return (military and military_enabled) or (not military and normal_enabled)


class ClosestAircraftFallback:
    """Maintain one bounded fallback snapshot on a start-based cadence."""

    def __init__(self, client, *, monotonic_clock=monotonic, wall_clock=time):
        self.client = client
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="fallback-adsb",
        )
        self.lock = Lock()
        self.future = None
        self.config = None
        self.generation = 0
        self.active = False
        self.snapshot = []
        self.next_due = 0
        self.status = "disabled"
        self.error = None
        self.http_status = None
        self.last_attempt_at = None
        self.last_success_at = None
        self.request_count = 0
        self.closed = False
        atexit.register(self.shutdown)

    def configure(
        self,
        *,
        enabled,
        provider,
        latitude,
        longitude,
        radius,
        normal_enabled,
        military_enabled,
    ):
        next_config = (
            bool(enabled), provider, float(latitude), float(longitude),
            int(radius), bool(normal_enabled), bool(military_enabled),
        )
        with self.lock:
            if next_config == self.config:
                return
            self.config = next_config
            self._invalidate("idle" if enabled else "disabled")

    def _invalidate(self, status):
        self.generation += 1
        self.active = False
        self.snapshot = []
        self.next_due = 0
        self.status = status
        self.error = None
        self.http_status = None
        if self.future is not None:
            self.future.cancel()

    def _fetch(
        self, generation, provider, latitude, longitude, radius,
        normal_enabled, military_enabled,
    ):
        if provider == "public" and normal_enabled and military_enabled:
            aircraft = self.client.GET_closest(
                radius, lat=latitude, lon=longitude,
            )
        else:
            aircraft = self.client.GET_point(
                radius, provider=provider, lat=latitude, lon=longitude,
            )
            aircraft = [
                item for item in aircraft
                if _mode_eligible(item, normal_enabled, military_enabled)
            ]
            aircraft.sort(key=lambda item: (
                (
                    float(item["range"])
                    if item.get("range") is not None
                    else float("inf")
                ),
                str(item.get("hex") or ""),
            ))
            aircraft = aircraft[:1]

        source = f"remote_fallback_{provider}"
        normalized = []
        for item in aircraft[:1]:
            result = dict(item)
            result["source"] = source
            if result.get("position_observed_at") is not None:
                result["position_source"] = source
            normalized.append(result)
        return generation, normalized

    def _finish(self, now_monotonic, now_wall):
        future = self.future
        if future is None or not future.done():
            return
        self.future = None
        try:
            generation, aircraft = future.result()
            if generation != self.generation or not self.active:
                return
            self.last_success_at = now_wall
            if aircraft:
                self.snapshot = aircraft
            cached_fresh = any(
                is_fresh(
                    item.get("position_observed_at"),
                    POSITION_FRESH_SECONDS,
                    now=now_wall,
                )
                for item in self.snapshot
            )
            self.status = "remote_fallback" if cached_fresh else "empty"
            self.error = None
            self.http_status = 200
        except CancelledError:
            return
        except requests.exceptions.HTTPError as error:
            response = getattr(error, "response", None)
            status_code = response.status_code if response is not None else None
            self.http_status = status_code
            if status_code in {401, 403}:
                delay = REMOTE_AUTH_RETRY_SECONDS
                self.status = "unauthorized"
            elif status_code == 429:
                delay = max(
                    REMOTE_RETRY_MIN_SECONDS,
                    retry_after_seconds(error, now=now_wall) or 0,
                )
                self.status = "rate_limited"
            else:
                delay = REMOTE_RETRY_MIN_SECONDS
                self.status = "error"
            self.next_due = now_monotonic + delay
            self.error = str(error)
            LOGGER.warning("Fallback aircraft request failed: %s", error)
        except (requests.exceptions.RequestException, ValueError) as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            self.http_status = None
            LOGGER.warning("Fallback aircraft request failed: %s", error)
        except Exception as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            self.http_status = None
            LOGGER.exception("Unexpected fallback aircraft failure")

    def tick(self, *, active):
        """Advance acquisition and return only actually fresh cached data."""
        now_monotonic = self.monotonic_clock()
        now_wall = self.wall_clock()
        with self.lock:
            if self.closed:
                return []
            enabled, provider, latitude, longitude, radius, normal, military = (
                self.config or (False, None, 0, 0, 0, False, False)
            )
            self._finish(now_monotonic, now_wall)
            should_run = bool(enabled and active and (normal or military))
            if not should_run:
                if self.active:
                    self._invalidate("local" if enabled else "disabled")
                else:
                    self.status = "local" if enabled else "disabled"
                return []

            self.active = True
            if self.future is None and now_monotonic >= self.next_due:
                generation = self.generation
                self.status = "requesting"
                self.last_attempt_at = now_wall
                self.request_count += 1
                self.next_due = now_monotonic + ADSB_LOL_CLOSEST_POLL_SECONDS
                self.future = self.executor.submit(
                    self._fetch, generation, provider, latitude, longitude,
                    radius, normal, military,
                )

            fresh = [
                dict(item) for item in self.snapshot
                if is_fresh(
                    item.get("position_observed_at"),
                    POSITION_FRESH_SECONDS,
                    now=now_wall,
                )
            ]
            if not fresh and self.snapshot and self.future is None:
                self.status = "stale"
            return fresh

    def diagnostics(self):
        with self.lock:
            now_monotonic = self.monotonic_clock()
            now_wall = self.wall_clock()
            provider = self.config[1] if self.config else None
            return {
                "provider": provider,
                "status": self.status,
                "in_flight": self.future is not None,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "next_request_at": (
                    None if self.next_due <= now_monotonic
                    else now_wall + self.next_due - now_monotonic
                ),
                "http_status": self.http_status,
                "error": self.error,
                "request_count": self.request_count,
            }

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.generation += 1
            if self.future is not None:
                self.future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

"""Rate-limited adsb.lol fallback for the lightweight Basic display."""

import atexit
from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging
from threading import Lock
from time import monotonic, time

import requests

from config import (
    ADSB_LOL_CLOSEST_POLL_SECONDS,
    REMOTE_AUTH_RETRY_SECONDS,
    REMOTE_RETRY_MIN_SECONDS,
)
from retry_after import retry_after_seconds


LOGGER = logging.getLogger("adsb_display.closest_fallback")


class ClosestAircraftFallback:
    """Maintain one shared, non-blocking closest-aircraft snapshot."""

    def __init__(self, client, *, monotonic_clock=monotonic, wall_clock=time):
        self.client = client
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="closest-adsb",
        )
        self.lock = Lock()
        self.future = None
        self.config = None
        self.generation = 0
        self.snapshot = []
        self.next_due = 0
        self.status = "idle"
        self.error = None
        self.closed = False
        atexit.register(self.shutdown)

    def _configure(self, latitude, longitude, radius):
        next_config = (float(latitude), float(longitude), int(radius))
        if next_config == self.config:
            return
        self.config = next_config
        self.generation += 1
        self.snapshot = []
        self.next_due = 0
        self.status = "idle"
        self.error = None
        if self.future is not None:
            self.future.cancel()

    def _fetch(self, generation, latitude, longitude, radius):
        return generation, self.client.GET_closest(
            radius,
            lat=latitude,
            lon=longitude,
        )

    def _finish(self, now_monotonic, now_wall):
        future = self.future
        if future is None or not future.done():
            return
        self.future = None
        try:
            generation, aircraft = future.result()
            if generation != self.generation:
                return
            self.snapshot = aircraft[:1]
            self.next_due = now_monotonic + ADSB_LOL_CLOSEST_POLL_SECONDS
            self.status = "ready"
            self.error = None
        except CancelledError:
            return
        except requests.exceptions.HTTPError as error:
            response = getattr(error, "response", None)
            status_code = response.status_code if response is not None else None
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
            LOGGER.warning("Closest-aircraft fallback failed: %s", error)
        except (requests.exceptions.RequestException, ValueError) as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            LOGGER.warning("Closest-aircraft fallback failed: %s", error)
        except Exception as error:
            self.next_due = now_monotonic + REMOTE_RETRY_MIN_SECONDS
            self.status = "error"
            self.error = str(error)
            LOGGER.exception("Unexpected closest-aircraft fallback failure")

    def tick(self, *, latitude, longitude, radius):
        """Start a due request and return the most recent shared snapshot."""
        now_monotonic = self.monotonic_clock()
        now_wall = self.wall_clock()
        with self.lock:
            if self.closed:
                return []
            self._configure(latitude, longitude, radius)
            self._finish(now_monotonic, now_wall)
            if self.future is None and now_monotonic >= self.next_due:
                self.status = "requesting"
                generation = self.generation
                self.future = self.executor.submit(
                    self._fetch,
                    generation,
                    float(latitude),
                    float(longitude),
                    int(radius),
                )
            return [dict(item) for item in self.snapshot]

    def disable(self):
        """Immediately discard cached data when network permission is off."""
        with self.lock:
            self.generation += 1
            self.snapshot = []
            self.next_due = 0
            self.status = "disabled"
            self.error = None
            if self.future is not None:
                self.future.cancel()

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.generation += 1
            if self.future is not None:
                self.future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

"""Bounded, non-blocking route enrichment keyed by normalized callsign."""

from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Lock
import time

import requests

from adsblol import ExternalLookupDisabled
from config import (
    DATABASE_PATH,
    ROUTE_LOOKUP_MAX_PENDING,
    ROUTE_LOOKUP_WORKERS,
    ROUTE_RETRY_MAX_SECONDS,
    ROUTE_RETRY_MIN_SECONDS,
)
from database import connect_database, ensure_aircraft_schema
from retry_after import retry_after_seconds


LOGGER = logging.getLogger("adsb_display.routes")
ROUTE_FIELDS = (
    "airline",
    "airline_icao",
    "departure",
    "departure_name",
    "destination",
    "destination_name",
)


def normalize_callsign(value):
    callsign = str(value or "").strip().upper()
    return "" if callsign in {"", "NA", "N/A"} else callsign


class RouteLookupCoordinator:
    def __init__(
        self,
        lookup,
        db_path=DATABASE_PATH,
        *,
        workers=ROUTE_LOOKUP_WORKERS,
        max_pending=ROUTE_LOOKUP_MAX_PENDING,
        clock=time.time,
    ):
        self.lookup = lookup
        self.db_path = db_path
        self.max_pending = max_pending
        self.clock = clock
        self.executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="route-lookup",
        )
        self.lock = Lock()
        self.in_flight = set()
        self.external_enabled = True
        self.permission_generation = 0
        self.counts = {
            "queued": 0,
            "completed": 0,
            "unavailable": 0,
            "failed": 0,
            "cache_hit": 0,
            "backoff_hit": 0,
            "queue_full": 0,
        }
        with connect_database(self.db_path) as conn:
            ensure_aircraft_schema(conn, create_table=True)

    def _cache(self, callsign):
        with connect_database(self.db_path, rows=True) as conn:
            row = conn.execute("""
                SELECT * FROM route_cache WHERE callsign = ?
            """, (callsign,)).fetchone()
        return dict(row) if row is not None else None

    def _store_complete(self, callsign, route):
        now = self.clock()
        values = [route.get(field) for field in ROUTE_FIELDS]
        with connect_database(self.db_path) as conn:
            conn.execute("""
                INSERT INTO route_cache (
                    callsign, airline, airline_icao, departure,
                    departure_name, destination, destination_name,
                    status, failure_count, retry_after, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', 0, NULL, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    airline = excluded.airline,
                    airline_icao = excluded.airline_icao,
                    departure = excluded.departure,
                    departure_name = excluded.departure_name,
                    destination = excluded.destination,
                    destination_name = excluded.destination_name,
                    status = 'complete',
                    failure_count = 0,
                    retry_after = NULL,
                    updated_at = excluded.updated_at
            """, (callsign, *values, now))

    def _store_failure(self, callsign, error):
        previous = self._cache(callsign) or {}
        failure_count = int(previous.get("failure_count") or 0) + 1
        guided_delay = retry_after_seconds(error, self.clock())
        exponential_delay = min(
            ROUTE_RETRY_MAX_SECONDS,
            ROUTE_RETRY_MIN_SECONDS * (2 ** min(failure_count - 1, 6)),
        )
        delay = max(
            ROUTE_RETRY_MIN_SECONDS,
            guided_delay if guided_delay is not None else exponential_delay,
        )
        retry_at = self.clock() + delay
        with connect_database(self.db_path) as conn:
            conn.execute("""
                INSERT INTO route_cache (
                    callsign, status, failure_count, retry_after, updated_at
                ) VALUES (?, 'retry', ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    status = 'retry',
                    failure_count = excluded.failure_count,
                    retry_after = excluded.retry_after,
                    updated_at = excluded.updated_at
            """, (callsign, failure_count, retry_at, self.clock()))
        return retry_at

    def _apply_to_aircraft(self, callsign, aircraft_hex, route):
        values = [route.get(field) for field in ROUTE_FIELDS]
        with connect_database(self.db_path) as conn:
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
                    AND callsign = ?
            """, (*values, aircraft_hex, callsign))

    def set_external_enabled(self, enabled):
        """Atomically update permission and invalidate already queued work."""
        enabled = bool(enabled)
        with self.lock:
            if enabled != self.external_enabled:
                self.external_enabled = enabled
                self.permission_generation += 1

    def is_external_enabled(self):
        with self.lock:
            return self.external_enabled

    def _run(self, callsign, aircraft_hex, permission_generation):
        try:
            with self.lock:
                permitted = (
                    self.external_enabled
                    and permission_generation == self.permission_generation
                )
            if not permitted:
                return
            route = self.lookup(callsign, aircraft_hex)
            self._store_complete(callsign, route)
            self._apply_to_aircraft(callsign, aircraft_hex, route)
            unavailable = route.get("departure") in {None, "NA", "N/A"}
            with self.lock:
                self.counts["unavailable" if unavailable else "completed"] += 1
        except ExternalLookupDisabled:
            # Permission cancellation is neither a provider failure nor an
            # unavailable route, so it must not affect cache/backoff state.
            pass
        except requests.exceptions.RequestException as error:
            retry_at = self._store_failure(callsign, error)
            with self.lock:
                self.counts["failed"] += 1
            LOGGER.warning(
                "Route lookup for %s failed; retry after %.0f: %s",
                callsign,
                retry_at,
                error,
            )
        except Exception:
            # Unexpected failures use the same bounded backoff rather than
            # allowing detail polling to create a tight retry loop.
            error = requests.exceptions.RequestException(
                "unexpected route lookup failure"
            )
            self._store_failure(callsign, error)
            with self.lock:
                self.counts["failed"] += 1
            LOGGER.exception("Unexpected route lookup failure for %s", callsign)
        finally:
            with self.lock:
                self.in_flight.discard(callsign)

    def get_or_enqueue(self, callsign, aircraft_hex, external_enabled):
        callsign = normalize_callsign(callsign)
        if not callsign:
            return {"status": "unavailable"}

        cached = self._cache(callsign)
        if cached and cached["status"] == "complete":
            route = {field: cached.get(field) for field in ROUTE_FIELDS}
            self._apply_to_aircraft(callsign, aircraft_hex, route)
            with self.lock:
                self.counts["cache_hit"] += 1
            unavailable = route.get("departure") in {None, "NA", "N/A"}
            return {
                "status": "unavailable" if unavailable else "ready",
                "route": route,
            }

        now = self.clock()
        if cached and cached["status"] == "retry" and (
            float(cached.get("retry_after") or 0) > now
        ):
            with self.lock:
                self.counts["backoff_hit"] += 1
            return {
                "status": "retrying",
                "retry_after": float(cached["retry_after"]),
            }

        if not external_enabled:
            return {"status": "disabled"}

        with self.lock:
            if not self.external_enabled:
                return {"status": "disabled"}
            if callsign in self.in_flight:
                return {"status": "loading"}
            if len(self.in_flight) >= self.max_pending:
                self.counts["queue_full"] += 1
                return {"status": "busy"}
            self.in_flight.add(callsign)
            self.counts["queued"] += 1
            permission_generation = self.permission_generation

        self.executor.submit(
            self._run,
            callsign,
            aircraft_hex,
            permission_generation,
        )
        return {"status": "loading"}

    def diagnostics(self):
        with self.lock:
            return {
                **self.counts,
                "in_flight": len(self.in_flight),
                "max_pending": self.max_pending,
            }

    def shutdown(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


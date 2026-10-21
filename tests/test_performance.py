import math
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

import requests

import app as web_app
from adsblol import ADSB_LOL
from config import HISTORY_MAX_POINTS
from database import connect_database, ensure_aircraft_schema, ensure_mode_settings
from route_enrichment import RouteLookupCoordinator, retry_after_seconds
import temp as collector


UNAVAILABLE_ROUTE = {
    "airline": "NA",
    "airline_icao": None,
    "departure": "NA",
    "departure_name": "NA",
    "destination": "NA",
    "destination_name": "NA",
}
KNOWN_ROUTE = {
    "airline": "Example Air",
    "airline_icao": "EXA",
    "departure": "AAA",
    "departure_name": "Airport A",
    "destination": "BBB",
    "destination_name": "Airport B",
}


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def prepare_database(path, callsign="EXA123"):
    with connect_database(path) as conn:
        ensure_aircraft_schema(conn, create_table=True)
        ensure_mode_settings(conn)
        conn.execute("""
            INSERT INTO adsb_data (hex, callsign, missed_updates)
            VALUES ('abc123', ?, 0)
        """, (callsign,))


class DatabaseLifecycleTests(unittest.TestCase):
    def test_connection_commits_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lifecycle.db"
            with connect_database(path) as conn:
                conn.execute("CREATE TABLE sample (value INTEGER)")
                conn.execute("INSERT INTO sample VALUES (1)")
                closed_connection = conn

            with self.assertRaises(sqlite3.ProgrammingError):
                closed_connection.execute("SELECT 1")
            with sqlite3.connect(path) as verification:
                self.assertEqual(
                    verification.execute("SELECT COUNT(*) FROM sample").fetchone()[0],
                    1,
                )

    def test_connection_rolls_back_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.db"
            with sqlite3.connect(path) as setup:
                setup.execute("CREATE TABLE sample (value INTEGER)")
            with self.assertRaises(RuntimeError):
                with connect_database(path) as conn:
                    conn.execute("INSERT INTO sample VALUES (1)")
                    rolled_back_connection = conn
                    raise RuntimeError("stop")
            with self.assertRaises(sqlite3.ProgrammingError):
                rolled_back_connection.execute("SELECT 1")
            with sqlite3.connect(path) as verification:
                self.assertEqual(
                    verification.execute("SELECT COUNT(*) FROM sample").fetchone()[0],
                    0,
                )


class NetworkSettingsTests(unittest.TestCase):
    def test_old_combined_setting_migrates_without_enabling_new_services(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.db"
            with sqlite3.connect(path) as conn:
                conn.execute("""
                    CREATE TABLE app_settings (
                        id INTEGER PRIMARY KEY,
                        normal_enabled INTEGER NOT NULL,
                        military_enabled INTEGER NOT NULL,
                        adsblol_feed_enabled INTEGER NOT NULL
                    )
                """)
                conn.execute("INSERT INTO app_settings VALUES (1, 1, 0, 1)")
            with connect_database(path, rows=True) as conn:
                ensure_mode_settings(conn)
                row = conn.execute("SELECT * FROM app_settings").fetchone()

        self.assertTrue(row["adsblol_upload_enabled"])
        self.assertTrue(row["remote_aircraft_enabled"])
        self.assertFalse(row["route_enrichment_enabled"])
        self.assertFalse(row["external_tiles_enabled"])

    def test_offline_missing_tile_performs_no_external_request(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            db_path = directory / "offline.db"
            with connect_database(db_path) as conn:
                ensure_mode_settings(conn)
                conn.execute("""
                    UPDATE app_settings
                    SET offline_mode_enabled = 1,
                        external_tiles_enabled = 1
                    WHERE id = 1
                """)

            def isolated_connection(*, rows=False):
                return connect_database(db_path, rows=rows)

            zoom = 12
            longitude = -117.11927031211901
            latitude = 33.512561467820056
            x = int((longitude + 180) / 360 * (2 ** zoom))
            latitude_rad = math.radians(latitude)
            y = int((1 - math.asinh(math.tan(latitude_rad)) / math.pi) / 2 * (2 ** zoom))
            with (
                patch.object(web_app, "connect_database", isolated_connection),
                patch.object(web_app, "TILE_CACHE_DIR", directory / "tiles"),
                patch.object(web_app.requests, "get") as external_get,
            ):
                response = web_app.app.test_client().get(
                    f"/tiles/{zoom}/{x}/{y}.png"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Local-Tile-Cache"], "OFFLINE-MISSING")
        external_get.assert_not_called()


class RouteCoordinatorTests(unittest.TestCase):
    def make_coordinator(self, path, lookup, clock=time.time):
        return RouteLookupCoordinator(
            lookup,
            path,
            workers=1,
            max_pending=4,
            clock=clock,
        )

    def test_offline_mode_never_enqueues_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            lookup = Mock(return_value=KNOWN_ROUTE)
            coordinator = self.make_coordinator(path, lookup)
            try:
                state = coordinator.get_or_enqueue("EXA123", "abc123", False)
            finally:
                coordinator.shutdown()
        self.assertEqual(state["status"], "disabled")
        lookup.assert_not_called()

    def test_concurrent_requests_for_callsign_share_one_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            release = Event()
            lookup = Mock(side_effect=lambda *_: (release.wait(), KNOWN_ROUTE)[1])
            coordinator = self.make_coordinator(path, lookup)
            try:
                first = coordinator.get_or_enqueue("exa123", "abc123", True)
                second = coordinator.get_or_enqueue("EXA123", "abc123", True)
                self.assertEqual(first["status"], "loading")
                self.assertEqual(second["status"], "loading")
                release.set()
                wait_for(lambda: coordinator.diagnostics()["in_flight"] == 0)
            finally:
                coordinator.shutdown()
        self.assertEqual(lookup.call_count, 1)

    def test_timeout_enters_backoff_instead_of_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            lookup = Mock(side_effect=requests.Timeout("slow"))
            coordinator = self.make_coordinator(path, lookup)
            try:
                coordinator.get_or_enqueue("EXA123", "abc123", True)
                wait_for(lambda: coordinator.diagnostics()["in_flight"] == 0)
                state = coordinator.get_or_enqueue("EXA123", "abc123", True)
            finally:
                coordinator.shutdown()
        self.assertEqual(state["status"], "retrying")
        self.assertEqual(lookup.call_count, 1)

    def test_unavailable_result_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            lookup = Mock(return_value=UNAVAILABLE_ROUTE)
            coordinator = self.make_coordinator(path, lookup)
            try:
                coordinator.get_or_enqueue("EXA123", "abc123", True)
                wait_for(lambda: coordinator.diagnostics()["in_flight"] == 0)
                state = coordinator.get_or_enqueue("EXA123", "abc123", True)
            finally:
                coordinator.shutdown()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(lookup.call_count, 1)

    def test_retry_after_rate_limit_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            now = [1000.0]
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "120"
            error = requests.HTTPError("limited", response=response)
            coordinator = self.make_coordinator(
                path,
                Mock(side_effect=error),
                clock=lambda: now[0],
            )
            try:
                coordinator.get_or_enqueue("EXA123", "abc123", True)
                wait_for(lambda: coordinator.diagnostics()["in_flight"] == 0)
                state = coordinator.get_or_enqueue("EXA123", "abc123", True)
            finally:
                coordinator.shutdown()
        self.assertEqual(state["status"], "retrying")
        self.assertEqual(state["retry_after"], 1120.0)

    def test_malformed_retry_after_is_ignored_safely(self):
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "not-a-delay"
        error = requests.HTTPError("limited", response=response)
        self.assertIsNone(retry_after_seconds(error, now=1000))

    def test_callsign_change_rejects_stale_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.db"
            prepare_database(path)
            release = Event()
            coordinator = self.make_coordinator(
                path,
                lambda *_: (release.wait(), KNOWN_ROUTE)[1],
            )
            try:
                coordinator.get_or_enqueue("EXA123", "abc123", True)
                with connect_database(path) as conn:
                    conn.execute(
                        "UPDATE adsb_data SET callsign = 'NEW456' WHERE hex = 'abc123'"
                    )
                release.set()
                wait_for(lambda: coordinator.diagnostics()["in_flight"] == 0)
                with connect_database(path, rows=True) as conn:
                    row = conn.execute(
                        "SELECT route_lookup_complete FROM adsb_data"
                    ).fetchone()
            finally:
                coordinator.shutdown()
        self.assertFalse(row["route_lookup_complete"])


class CollectorAndHistoryTests(unittest.TestCase):
    @staticmethod
    def aircraft(latitude=33.5):
        return {
            "hex": "abc123",
            "callsign": "TEST1",
            "t": "C172",
            "r": "N123AB",
            "lat": latitude,
            "lon": -117.1,
            "range": 2,
            "alt_baro": 1000,
            "gs": 90,
            "track": 180,
            "type": "NA",
            "category": "Light Aircraft",
            "flag": [],
            "airline": None,
            "airline_icao": None,
            "departure": None,
            "departure_name": None,
            "destination": None,
            "destination_name": None,
        }

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            database = collector.Database(Path(directory) / "history.db")
            timestamps = [1000 + index * 6 for index in range(HISTORY_MAX_POINTS + 20)]
            with patch.object(collector, "time", side_effect=timestamps):
                for index in range(len(timestamps)):
                    database.update_ac([self.aircraft(33.5 + index * 0.0001)])
            with database.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM aircraft_positions"
                ).fetchone()[0]
        # Pruning is intentionally batched once per minute to reduce writes.
        self.assertLessEqual(count, HISTORY_MAX_POINTS + 10)

    def test_returned_trail_is_bounded_and_chronological(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trail.db"
            prepare_database(path)
            with connect_database(path) as conn:
                aircraft_id = conn.execute(
                    "SELECT id FROM adsb_data WHERE hex = 'abc123'"
                ).fetchone()[0]
                conn.executemany("""
                    INSERT INTO aircraft_positions (
                        aircraft_id, recorded_at, lat, lon
                    ) VALUES (?, ?, ?, ?)
                """, [
                    (aircraft_id, index, 33 + index / 10000, -117)
                    for index in range(HISTORY_MAX_POINTS + 40)
                ])

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                trail = web_app.get_aircraft_path("abc123")

        self.assertEqual(len(trail), HISTORY_MAX_POINTS)
        self.assertEqual(trail[0]["recorded_at"], 40)
        self.assertEqual(trail[-1]["recorded_at"], HISTORY_MAX_POINTS + 39)

    def test_remote_failure_does_not_drop_local_collection(self):
        class FakeDatabase:
            def __init__(self):
                self.updated = None

            def get_collection_settings(self):
                return (True, True, 50, 33.5, -117.1, True, "local", 30005,
                        False, True, False)

            def enrich_local_aircraft(self, payload):
                return payload

            def update_ac(self, data):
                self.updated = data

        fake_database = FakeDatabase()
        local_payload = {"ac": [self.aircraft()]}
        adsb = ADSB_LOL(lat=33.5, lon=-117.1)
        adsb.GET_point = Mock(side_effect=requests.ConnectionError("offline"))
        with (
            patch.object(collector, "Database", return_value=fake_database),
            patch.object(
                collector.LocalReceiver,
                "get_aircraft",
                return_value=local_payload,
            ),
            patch.object(collector.ADSBLolFeeder, "sync"),
            patch.object(collector, "sleep", side_effect=StopIteration),
            self.assertRaises(StopIteration),
        ):
            collector.run_collector(adsb)

        self.assertEqual(len(fake_database.updated), 1)
        self.assertEqual(fake_database.updated[0]["hex"], "abc123")

    def test_upload_alone_does_not_enable_remote_polling(self):
        class FakeDatabase:
            def get_collection_settings(self):
                return (True, True, 50, 33.5, -117.1, True, "local", 30005,
                        True, False, False)

            def enrich_local_aircraft(self, payload):
                return payload

            def update_ac(self, data):
                self.updated = data

        fake_database = FakeDatabase()
        adsb = ADSB_LOL(lat=33.5, lon=-117.1)
        adsb.GET_point = Mock(return_value=[])
        feeder_sync = Mock()
        with (
            patch.object(collector, "Database", return_value=fake_database),
            patch.object(
                collector.LocalReceiver,
                "get_aircraft",
                return_value={"ac": [self.aircraft()]},
            ),
            patch.object(collector.ADSBLolFeeder, "sync", feeder_sync),
            patch.object(collector, "sleep", side_effect=StopIteration),
            self.assertRaises(StopIteration),
        ):
            collector.run_collector(adsb)

        adsb.GET_point.assert_not_called()
        feeder_sync.assert_called_once_with(True, "local", 30005)

    def test_offline_mode_disables_upload_and_remote_but_keeps_local(self):
        class FakeDatabase:
            def get_collection_settings(self):
                return (True, True, 50, 33.5, -117.1, True, "local", 30005,
                        True, True, True)

            def enrich_local_aircraft(self, payload):
                return payload

            def update_ac(self, data):
                self.updated = data

        fake_database = FakeDatabase()
        adsb = ADSB_LOL(lat=33.5, lon=-117.1)
        adsb.GET_point = Mock(return_value=[])
        feeder_sync = Mock()
        with (
            patch.object(collector, "Database", return_value=fake_database),
            patch.object(
                collector.LocalReceiver,
                "get_aircraft",
                return_value={"ac": [self.aircraft()]},
            ),
            patch.object(collector.ADSBLolFeeder, "sync", feeder_sync),
            patch.object(collector, "sleep", side_effect=StopIteration),
            self.assertRaises(StopIteration),
        ):
            collector.run_collector(adsb)

        adsb.GET_point.assert_not_called()
        feeder_sync.assert_called_once_with(False, "local", 30005)
        self.assertEqual(len(fake_database.updated), 1)


class FrontendRegressionTests(unittest.TestCase):
    def test_constrained_profile_and_keyed_updates_are_present(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "static" / "dashboard.js").read_text()
        css = (root / "static" / "dashboard.css").read_text()
        self.assertIn("const aircraftRows = new Map()", javascript)
        self.assertNotIn("marker.unbindTooltip()", javascript)
        self.assertIn("marker.aircraftIconKey !== iconKey", javascript)
        self.assertIn("if (document.hidden || aircraftRefreshInFlight)", javascript)
        self.assertIn("body.constrained-device .leaflet-tile-pane", css)


if __name__ == "__main__":
    unittest.main()

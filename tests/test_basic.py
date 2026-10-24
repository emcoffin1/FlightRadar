from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import app as web_app
from database import connect_database, ensure_aircraft_schema, ensure_mode_settings


class BasicDisplayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_basic_client_behavior(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ["basic_display_test.js", "animation_loop_test.js"]:
            with self.subTest(filename=filename):
                result = subprocess.run(
                    ["node", str(root / "tests" / filename)],
                    cwd=root, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "basic.db"
        self.now = time.time()
        with connect_database(self.path) as conn:
            ensure_aircraft_schema(conn, create_table=True)
            ensure_mode_settings(conn)
            conn.execute("UPDATE collector_status SET heartbeat_at = ? WHERE id = 1", (self.now,))
            for hex_value, distance, age in [("near01", 1, 0), ("far001", 4, 0), ("old001", .2, 100), ("out001", 999, 0)]:
                conn.execute("""
                    INSERT INTO adsb_data (hex, callsign, r, t, range, lat, lon,
                        position_observed_at, source)
                    VALUES (?, ?, 'N123AB', 'C172', ?, 33, -118, ?, 'local')
                """, (hex_value, hex_value.upper(), distance, self.now - age))
        def connection(*, rows=False):
            return connect_database(self.path, rows=rows)
        patcher = patch.object(web_app, "connect_database", connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = web_app.app.test_client()

    def test_nearest_snapshot_includes_image_and_airport_route(self):
        route = {
            "route_status": "ready",
            "departure": "LAX",
            "departure_name": "Los Angeles International Airport",
            "destination": "SFO",
            "destination_name": "San Francisco International Airport",
        }

        def resolve(aircraft):
            return {**aircraft, **route}

        with (
            patch.object(web_app, "resolve_aircraft_route", side_effect=resolve),
            patch.object(
                web_app,
                "aircraft_image_url",
                return_value="/static/aircraft_photos/c172.webp",
            ),
        ):
            response = self.client.get("/api/aircraft/nearest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        aircraft = response.get_json()["aircraft"]
        self.assertEqual(aircraft["hex"], "near01")
        self.assertEqual(aircraft["r"], "N123AB")
        self.assertEqual(aircraft["departure"], "LAX")
        self.assertEqual(aircraft["destination_name"], route["destination_name"])
        self.assertEqual(
            aircraft["aircraft_image"],
            "/static/aircraft_photos/c172.webp",
        )
        self.assertNotIn("path", aircraft)

    def test_departed_aircraft_switches_to_next_fresh_contact(self):
        with connect_database(self.path) as conn:
            conn.execute("UPDATE adsb_data SET position_observed_at = ? WHERE hex = 'near01'", (self.now - 100,))
        self.assertEqual(self.client.get("/api/aircraft/nearest").get_json()["aircraft"]["hex"], "far001")

    def test_stale_collector_and_empty_snapshot_clear_aircraft(self):
        with connect_database(self.path) as conn:
            conn.execute("UPDATE collector_status SET heartbeat_at = ?", (self.now - 100,))
        payload = self.client.get("/api/aircraft/nearest").get_json()
        self.assertIsNone(payload["aircraft"])
        self.assertFalse(payload["collector_fresh"])
        with connect_database(self.path) as conn:
            conn.execute("UPDATE collector_status SET heartbeat_at = ?", (self.now,))
            conn.execute("DELETE FROM adsb_data")
        payload = self.client.get("/api/aircraft/nearest").get_json()
        self.assertIsNone(payload["aircraft"])
        self.assertTrue(payload["collector_fresh"])

    def test_empty_snapshot_uses_online_closest_fallback_when_allowed(self):
        with connect_database(self.path) as conn:
            conn.execute("DELETE FROM adsb_data")
            conn.execute("""
                UPDATE app_settings
                SET remote_aircraft_enabled = 1, offline_mode_enabled = 0
                WHERE id = 1
            """)
        fallback = {
            "hex": "remote1", "callsign": "REMOTE1", "t": "BE95",
            "r": "N95", "range": 8.5, "lat": 33.1, "lon": -118.1,
            "alt_baro": 5000, "gs": 140, "track": 270,
            "source": "remote_closest", "category": "Small Aircraft",
            "position_observed_at": self.now,
        }
        with (
            patch.object(web_app.closest_aircraft_fallback, "tick", return_value=[fallback]) as tick,
            patch.object(web_app, "resolve_aircraft_route", side_effect=lambda aircraft: aircraft),
        ):
            payload = self.client.get("/api/aircraft/nearest?online=1").get_json()
        self.assertEqual(payload["aircraft"]["hex"], "remote1")
        self.assertEqual(payload["live_source"], "adsblol_closest")
        tick.assert_called_once()

    def test_closest_fallback_respects_offline_browser_and_settings(self):
        with connect_database(self.path) as conn:
            conn.execute("DELETE FROM adsb_data")
            conn.execute("""
                UPDATE app_settings
                SET remote_aircraft_enabled = 1, offline_mode_enabled = 0
                WHERE id = 1
            """)
        with patch.object(web_app.closest_aircraft_fallback, "tick", return_value=[]) as tick:
            self.client.get("/api/aircraft/nearest?online=0")
            tick.assert_not_called()

        with connect_database(self.path) as conn:
            conn.execute("UPDATE app_settings SET offline_mode_enabled = 1 WHERE id = 1")
        with (
            patch.object(web_app.closest_aircraft_fallback, "tick", return_value=[]) as tick,
            patch.object(web_app.closest_aircraft_fallback, "disable") as disable,
        ):
            self.client.get("/api/aircraft/nearest?online=1")
            tick.assert_not_called()
            disable.assert_called_once()

    def test_basic_document_loads_only_lightweight_scripts(self):
        page = self.client.get("/basic").get_data(as_text=True)
        self.assertIn("basic.js", page)
        self.assertIn('id="aircraft-image"', page)
        self.assertIn('aria-label="Airport locations"', page)
        for heavy_resource in ["leaflet.js", "dashboard.js", "detail_scheduler.js", "aircraft_photos", "fonts/"]:
            self.assertNotIn(heavy_resource, page)
        self.assertIn('href="/"', page)


if __name__ == "__main__":
    unittest.main()

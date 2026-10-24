import json
import gzip
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import app as web_app
from adsblol import ADSB_LOL
from database import (
    connect_database,
    decode_database_flags,
    ensure_aircraft_metadata,
    ensure_mode_settings,
)
from local_receiver import (
    LocalReceiver,
    receiver_beast_host,
    receiver_json_location,
)
from temp import filter_modes, merge_aircraft


class ReceiverAddressTests(unittest.TestCase):
    def test_local_path_is_preserved(self):
        location = receiver_json_location("/run/readsb/aircraft.json")
        self.assertEqual(location, Path("/run/readsb/aircraft.json"))
        self.assertEqual(receiver_beast_host(str(location)), "127.0.0.1")

    def test_hostname_gets_standard_readsb_url(self):
        self.assertEqual(
            receiver_json_location("pi-zero.local"),
            "http://pi-zero.local/readsb/aircraft.json",
        )
        self.assertEqual(receiver_beast_host("pi-zero.local"), "pi-zero.local")

    def test_full_url_and_web_port_are_supported(self):
        url = "http://192.168.1.20:8080/aircraft.json"
        self.assertEqual(receiver_json_location(url), url)
        self.assertEqual(receiver_beast_host(url), "192.168.1.20")

    def test_unsupported_url_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            receiver_json_location("ftp://receiver/aircraft.json")


class LocalDataTests(unittest.TestCase):
    def test_readsb_json_is_loaded_and_stale_contacts_are_removed(self):
        payload = {
            "now": time.time(),
            "aircraft": [
                {"hex": "abc123", "seen": 0.2},
                {"hex": "def456", "seen": 20},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aircraft.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = LocalReceiver().get_aircraft(str(path))

        self.assertEqual([item["hex"] for item in result["ac"]], ["abc123"])

    def test_readsb_aircraft_format_matches_existing_api_shape(self):
        client = ADSB_LOL(lat=33.77, lon=-118.12)
        now = time.time()
        result = client._filter_format({
            "now": now,
            "ac": [{
                "hex": "ae1234",
                "flight": "RCH123 ",
                "lat": 33.8,
                "lon": -118.1,
                "alt_baro": 12000,
                "track": 90,
                "gs": 310,
                "dbFlags": 1,
                "seen": 0.1,
                "seen_pos": 0.2,
            }],
        }, max_range=50)

        self.assertEqual(result[0]["callsign"], "RCH123")
        self.assertEqual(result[0]["flag"], ["Military"])
        self.assertEqual(result[0]["t"], "NA")
        self.assertLess(result[0]["range"], 50)

    def test_local_values_win_and_remote_metadata_fills_gaps(self):
        remote = [{
            "hex": "abc123", "lat": 1, "lon": 2, "t": "C17",
            "r": "01-0001", "flag": ["Military"],
            "position_observed_at": 100,
        }]
        local = [{
            "hex": "abc123", "lat": 3, "lon": 4, "t": "NA",
            "r": "NA", "flag": ["NA"],
            "position_observed_at": 200,
        }]
        result = merge_aircraft(remote, local)[0]
        self.assertEqual((result["lat"], result["lon"]), (3, 4))
        self.assertEqual(result["t"], "C17")
        self.assertEqual(result["flag"], ["Military"])
        self.assertEqual(filter_modes([result], False, True), [result])


class SettingsMigrationTests(unittest.TestCase):
    def test_receiver_settings_are_added_to_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            with connect_database(path, rows=True) as connection:
                connection.execute("""
                    CREATE TABLE app_settings (
                        id INTEGER PRIMARY KEY,
                        normal_enabled INTEGER NOT NULL,
                        military_enabled INTEGER NOT NULL,
                        center_lat REAL NOT NULL,
                        center_lon REAL NOT NULL,
                        search_range_nm INTEGER NOT NULL
                    )
                """)
                connection.execute(
                    "INSERT INTO app_settings VALUES (1, 1, 0, 1, 2, 25)"
                )
                ensure_mode_settings(connection)
                row = connection.execute(
                    "SELECT * FROM app_settings WHERE id = 1"
                ).fetchone()

        self.assertEqual(row["receiver_address"], "/run/readsb/aircraft.json")
        self.assertEqual(row["receiver_beast_port"], 30005)
        self.assertTrue(row["local_receiver_enabled"])
        self.assertFalse(row["adsblol_feed_enabled"])
        self.assertEqual(row["remote_endpoint_preference"], "auto")

    def test_coordinate_api_preserves_receiver_settings_for_old_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path, rows=True) as connection:
                ensure_mode_settings(connection)
                connection.execute("""
                    UPDATE app_settings
                    SET local_receiver_enabled = 1,
                        receiver_address = 'receiver.local',
                        receiver_beast_port = 30006,
                        adsblol_feed_enabled = 1,
                        adsblol_upload_enabled = 1,
                        remote_aircraft_enabled = 1
                    WHERE id = 1
                """)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                client = web_app.app.test_client()
                response = client.patch(
                    "/api/settings/coordinates",
                    json={"lat": 33.77, "lon": -118.12},
                )

        self.assertEqual(response.status_code, 200)
        settings = response.get_json()
        self.assertTrue(settings["local_receiver_enabled"])
        self.assertEqual(settings["receiver_address"], "receiver.local")
        self.assertEqual(settings["receiver_beast_port"], 30006)
        self.assertTrue(settings["adsblol_feed_enabled"])

    def test_coordinate_api_updates_receiver_and_feeder_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path, rows=True) as connection:
                ensure_mode_settings(connection)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                client = web_app.app.test_client()
                response = client.patch(
                    "/api/settings/coordinates",
                    json={
                        "lat": 33.7719585,
                        "lon": -118.1193111,
                        "local_receiver_enabled": True,
                        "receiver_address": "http://pi-zero:8080/aircraft.json",
                        "receiver_beast_port": 30005,
                        "adsblol_enabled": True,
                    },
                )

        self.assertEqual(response.status_code, 200)
        settings = response.get_json()
        self.assertTrue(settings["local_receiver_enabled"])
        self.assertTrue(settings["adsblol_enabled"])
        self.assertEqual(settings["receiver_beast_port"], 30005)

    def test_coordinate_api_updates_adsblol_toggle_without_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path, rows=True) as connection:
                ensure_mode_settings(connection)
                before = connection.execute("""
                    SELECT center_lat, center_lon
                    FROM app_settings
                    WHERE id = 1
                """).fetchone()

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                client = web_app.app.test_client()
                response = client.patch(
                    "/api/settings/coordinates",
                    json={"adsblol_enabled": True},
                )

        self.assertEqual(response.status_code, 200)
        settings = response.get_json()
        self.assertTrue(settings["adsblol_enabled"])
        self.assertEqual(settings["lat"], before["center_lat"])
        self.assertEqual(settings["lon"], before["center_lon"])

    def test_coordinate_api_persists_remote_endpoint_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path) as connection:
                ensure_mode_settings(connection)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                response = web_app.app.test_client().patch(
                    "/api/settings/coordinates",
                    json={"remote_endpoint_preference": "feeder"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["remote_endpoint_preference"], "feeder"
        )

    def test_coordinate_api_rejects_unknown_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path) as connection:
                ensure_mode_settings(connection)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                response = web_app.app.test_client().patch(
                    "/api/settings/coordinates",
                    json={"remote_endpoint_preference": "fallback-magic"},
                )
        self.assertEqual(response.status_code, 400)

    def test_dashboard_renders_receiver_controls_and_one_second_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            with connect_database(path, rows=True) as connection:
                ensure_mode_settings(connection)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                response = web_app.app.test_client().get("/")

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="local-receiver-enabled"', page)
        self.assertIn('id="adsblol-upload-enabled"', page)
        self.assertIn('id="remote-aircraft-enabled"', page)
        self.assertIn('id="route-enrichment-enabled"', page)
        self.assertIn('id="remote-endpoint-preference"', page)
        self.assertIn('id="external-tiles-enabled"', page)
        self.assertIn('id="offline-mode-enabled"', page)
        self.assertIn('id="coordinate-lat"', page)
        self.assertIn('id="coordinate-keypad"', page)
        self.assertIn('data-coordinate-action="backspace"', page)
        self.assertIn('inputmode="decimal"', page)
        self.assertNotIn('name="lat"\n                            type="number"', page)
        self.assertIn("aircraftRefreshMs: 1000", page)
        self.assertIn("aircraftDetailRefreshMs: 7000", page)
        self.assertLess(
            page.index("detail_scheduler.js"),
            page.index("dashboard.js"),
        )


class AircraftMetadataTests(unittest.TestCase):
    def test_flag_string_uses_readsb_bit_order(self):
        self.assertEqual(decode_database_flags("10"), 1)
        self.assertEqual(decode_database_flags("01"), 2)
        self.assertEqual(decode_database_flags("1011"), 13)

    def test_metadata_file_imports_type_registration_and_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            metadata_path = directory / "aircraft.csv.gz"
            with gzip.open(metadata_path, "wt", encoding="utf-8") as output:
                output.write(
                    "abc123;N123AB;B407;10;Bell 407;2004;Example Owner;\n"
                )
            with connect_database(directory / "metadata.db", rows=True) as conn:
                count = ensure_aircraft_metadata(conn, metadata_path)
                row = conn.execute("""
                    SELECT registration, type_code, db_flags
                    FROM aircraft_metadata
                    WHERE hex = 'abc123'
                """).fetchone()

        self.assertEqual(count, 1)
        self.assertEqual(tuple(row), ("N123AB", "B407", 1))


class AircraftAssetTests(unittest.TestCase):
    def test_common_unmapped_families_now_have_local_photos(self):
        expected = {
            "BE95": "baron.jpg",
            "M20P": "mooney.jpg",
            "DA40": "da40.jpg",
            "C310": "cessna_twin.jpg",
            "GLID": "glider.jpg",
            "BALL": "balloon.jpg",
            "ULAC": "ultralight.jpg",
            "B06": "bell407.webp",
            "BE20": "kingair.webp",
            "RV6": "rv7.webp",
        }
        with web_app.app.test_request_context():
            for type_code, filename in expected.items():
                with self.subTest(type_code=type_code):
                    self.assertTrue(
                        web_app.aircraft_image_url(type_code).endswith(filename)
                    )

    def test_common_helicopter_types_use_rotorcraft_assets(self):
        with web_app.app.test_request_context():
            self.assertTrue(web_app.aircraft_icon_url("B407").endswith("R44.svg"))
            self.assertTrue(web_app.aircraft_icon_url("A139").endswith("EC45.svg"))

    def test_unknown_rotorcraft_gets_generic_helicopter_assets(self):
        with web_app.app.test_request_context():
            aircraft = web_app.serialize_aircraft({
                "hex": "abc123",
                "callsign": "TEST",
                "t": "ZZZZ",
                "category": "Rotorcraft/Helicopter",
                "airline_icao": None,
                "r": None,
                "route_lookup_complete": 0,
                "airline": None,
                "departure": None,
                "destination": None,
            })

        self.assertTrue(aircraft["aircraft_icon"].endswith("R44.svg"))
        self.assertTrue(aircraft["aircraft_image"].endswith("r44.webp"))


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

import requests

import app as web_app
from adsblol import ADSB_LOL
from config import POSITION_FRESH_SECONDS, REMOTE_RETRY_MIN_SECONDS
from database import (
    COLLECTOR_STATUS_FIELDS,
    connect_database,
    ensure_aircraft_schema,
    ensure_mode_settings,
)
from local_receiver import ADSBLolFeeder, LocalReceiver
from temp import Database, RemoteAircraftPoller, merge_aircraft


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def formatted_aircraft(now, **updates):
    item = {
        "hex": "abc123",
        "callsign": "TEST1",
        "t": "C172",
        "r": "N123AB",
        "lat": 33.5,
        "lon": -117.1,
        "range": 2,
        "alt_baro": 1000,
        "gs": 90,
        "track": 180,
        "type": "adsb_icao",
        "category": "Light Aircraft",
        "flag": [],
        "airline": None,
        "airline_icao": None,
        "departure": None,
        "departure_name": None,
        "destination": None,
        "destination_name": None,
        "message_observed_at": now,
        "position_observed_at": now,
        "snapshot_generated_at": now,
        "source_fetched_at": now,
        "source": "local",
        "position_source": "local",
    }
    item.update(updates)
    return item


class TimestampSemanticsTests(unittest.TestCase):
    def load_local(self, aircraft, now=1_800_000_000):
        payload = {"now": now, "aircraft": aircraft}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aircraft.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("local_receiver.time.time", return_value=now):
                return LocalReceiver().get_aircraft(path)

    def test_recent_message_with_old_position_is_not_live(self):
        result = self.load_local([{
            "hex": "abc123", "seen": 1, "seen_pos": 600,
            "lat": 33.5, "lon": -117.1,
        }])
        item = result["ac"][0]
        self.assertIsNotNone(item["_message_observed_at"])
        self.assertIsNone(item["_position_observed_at"])
        self.assertNotIn("lat", item)

    def test_historical_and_estimated_positions_are_not_promoted(self):
        result = self.load_local([{
            "hex": "abc123", "seen": 1,
            "lastPosition": {"lat": 33.5, "lon": -117.1, "seen_pos": 3600},
            "rr_lat": 33.6, "rr_lon": -117.2,
        }])
        item = result["ac"][0]
        self.assertNotIn("lat", item)
        self.assertIsNone(item["_position_observed_at"])

    def test_invalid_missing_future_and_delayed_timestamps_are_safe(self):
        for value in (None, "bad", 1_800_000_100, 1_799_999_000):
            with self.subTest(value=value):
                payload = {"now": value, "aircraft": []}
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "aircraft.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with (
                        patch("local_receiver.time.time", return_value=1_800_000_000),
                        self.assertRaises(ValueError),
                    ):
                        LocalReceiver().get_aircraft(path)

    def test_repeated_poll_does_not_create_duplicate_trail_point(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "freshness.db")
            item = formatted_aircraft(1_800_000_000)
            with patch("temp.time", return_value=1_800_000_010):
                database.update_ac([item])
                database.update_ac([item])
            with database.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM aircraft_positions"
                ).fetchone()[0]
                observed = conn.execute(
                    "SELECT position_observed_at FROM adsb_data"
                ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(observed, 1_800_000_000)

    def test_merge_chooses_fresher_position_and_keeps_metadata(self):
        remote = formatted_aircraft(
            200, source="remote_feeder", position_source="remote_feeder",
            t="C17", r="01-0001", flag=["Military"], lat=1, lon=2,
        )
        local = formatted_aircraft(
            100, source="local", position_source="local",
            t="NA", r="NA", flag=["NA"], lat=3, lon=4,
        )
        result = merge_aircraft([remote], [local])[0]
        self.assertEqual((result["lat"], result["lon"]), (1, 2))
        self.assertEqual(result["t"], "C17")
        self.assertEqual(result["position_source"], "remote_feeder")

    def test_long_gap_starts_new_trail_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "segments.db")
            now = time.time()
            with patch("temp.time", return_value=now):
                database.update_ac([formatted_aircraft(now, lat=33.5)])
            with patch("temp.time", return_value=now + 201):
                database.update_ac([formatted_aircraft(now + 201, lat=33.51)])
            with database.connect() as conn:
                segments = [row[0] for row in conn.execute(
                    "SELECT segment_id FROM aircraft_positions ORDER BY id"
                )]
        self.assertEqual(segments, [1, 2])

    def test_implausible_jump_starts_new_trail_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "jump.db")
            now = time.time()
            with patch("temp.time", return_value=now):
                database.update_ac([
                    formatted_aircraft(now, lat=33.5, lon=-117.1)
                ])
            with patch("temp.time", return_value=now + 6):
                database.update_ac([
                    formatted_aircraft(now + 6, lat=38.5, lon=-122.1)
                ])
            with database.connect() as conn:
                segments = [row[0] for row in conn.execute(
                    "SELECT segment_id FROM aircraft_positions ORDER BY id"
                )]
        self.assertEqual(segments, [1, 2])

    def test_older_remote_position_cannot_mix_with_newer_local_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "position.db")
            now = time.time()
            local = formatted_aircraft(
                now, lat=33.5, lon=-117.1, range=2, alt_baro=1200,
                track=90, gs=110, source="local", position_source="local",
            )
            remote = formatted_aircraft(
                now - 5, lat=40, lon=-70, range=900, alt_baro=30000,
                track=270, gs=500, source="remote_public",
                position_source="remote_public",
            )
            with patch("temp.time", return_value=now):
                database.update_ac([local])
                database.update_ac([remote])
            with database.connect() as conn:
                row = conn.execute("""
                    SELECT lat, lon, range, alt_baro, track, gs,
                        position_observed_at, position_source
                    FROM adsb_data
                """).fetchone()

        self.assertEqual(
            tuple(row),
            (33.5, -117.1, 2.0, 1200, 90.0, 110.0, now, "local"),
        )

    def test_equal_timestamp_prefers_local_and_newer_local_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "sources.db")
            now = time.time()
            remote = formatted_aircraft(
                now, lat=34, lon=-118, range=5,
                source="remote_fallback_public",
                position_source="remote_fallback_public",
            )
            local = formatted_aircraft(
                now, lat=33, lon=-117, range=1,
                source="local", position_source="local",
            )
            delayed_remote = formatted_aircraft(
                now, lat=35, lon=-119, range=10,
                source="remote_public", position_source="remote_public",
            )
            recovered_local = formatted_aircraft(
                now + 1, lat=32, lon=-116, range=0.5,
                source="local", position_source="local",
            )
            with patch("temp.time", return_value=now + 1):
                database.update_ac([remote])
                database.update_ac([local])
                database.update_ac([delayed_remote])
                database.update_ac([recovered_local])
            with database.connect() as conn:
                row = conn.execute("""
                    SELECT lat, lon, range, position_observed_at,
                        position_source
                    FROM adsb_data
                """).fetchone()

        self.assertEqual(
            tuple(row),
            (32.0, -116.0, 0.5, now + 1, "local"),
        )

    def test_stale_or_invalid_position_is_never_inserted_as_live(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "invalid.db")
            now = time.time()
            stale = formatted_aircraft(
                now - POSITION_FRESH_SECONDS - 1,
                lat=91,
                lon=float("nan"),
                range=-1,
                source="remote_public",
                position_source="remote_public",
            )
            with patch("temp.time", return_value=now):
                database.update_ac([stale])
            with database.connect() as conn:
                row = conn.execute("""
                    SELECT lat, lon, range, position_observed_at,
                        position_source
                    FROM adsb_data
                """).fetchone()

        self.assertEqual(tuple(row), (None, None, None, None, None))


class RemoteProviderTests(unittest.TestCase):
    def response(self, payload, status=200):
        response = Mock(status_code=status)
        response.raise_for_status.side_effect = None
        response.json.return_value = payload
        return response

    def test_public_and_feeder_schemas_normalize(self):
        now = 1_800_000_000
        aircraft = [{
            "hex": "abc123", "flight": "TEST1", "lat": 33.5,
            "lon": -117.1, "seen": 1, "seen_pos": 2, "t": "C172",
        }]
        client = ADSB_LOL(lat=33.5, lon=-117.1)
        with patch("adsblol.time.time", return_value=now):
            client._external_get = Mock(side_effect=[
                self.response({"now": now * 1000, "ac": aircraft}),
                self.response({"now": now, "aircraft": aircraft}),
            ])
            public = client.GET_point(50, provider="public")
            feeder = client.GET_point(50, provider="feeder")
        self.assertEqual(public[0]["position_source"], "remote_public")
        self.assertEqual(feeder[0]["position_source"], "remote_feeder")
        self.assertEqual(public[0]["position_observed_at"], now - 2)
        self.assertEqual(feeder[0]["position_observed_at"], now - 2)
        self.assertIn("/v2/point/", client._external_get.call_args_list[0].args[1])
        self.assertIn("?circle=33.5,-117.1,50", client._external_get.call_args_list[1].args[1])

    def test_malformed_aircraft_is_isolated_and_fields_are_normalized(self):
        now = 1_800_000_000
        client = ADSB_LOL(lat=33.5, lon=-117.1)
        payload = {
            "now": now,
            "ac": [
                None,
                {"hex": "", "seen": 0},
                {"hex": "old123", "seen": "not-a-number"},
                {
                    "hex": 123456,
                    "flight": 789,
                    "seen": 0,
                    "seen_pos": 0,
                    "lat": 91,
                    "lon": float("inf"),
                    "alt_baro": float("nan"),
                    "track": float("inf"),
                    "gs": -1,
                    "t": None,
                    "r": None,
                },
                {
                    "hex": "GOOD01",
                    "flight": " SAFE1 ",
                    "seen": 0,
                    "seen_pos": 0,
                    "lat": 0,
                    "lon": 0,
                    "track": 450,
                    "gs": "120.5",
                    "alt_baro": "ground",
                },
            ],
        }

        result = client._filter_format(
            payload,
            fetched_at=now,
            source="remote_public",
        )

        self.assertEqual([item["hex"] for item in result], ["123456", "good01"])
        malformed = result[0]
        self.assertEqual(malformed["callsign"], "789")
        self.assertIsNone(malformed["lat"])
        self.assertIsNone(malformed["position_observed_at"])
        self.assertEqual(malformed["alt_baro"], "NA")
        self.assertEqual(malformed["track"], "NA")
        self.assertEqual(malformed["gs"], "NA")
        valid = result[1]
        self.assertEqual((valid["lat"], valid["lon"]), (0.0, 0.0))
        self.assertEqual(valid["track"], 90.0)
        self.assertEqual(valid["gs"], 120.5)
        self.assertEqual(valid["alt_baro"], "ground")

    def test_malformed_top_level_and_query_coordinates_are_rejected(self):
        client = ADSB_LOL()
        with self.assertRaisesRegex(ValueError, "not a JSON object"):
            client._filter_format([], fetched_at=1_800_000_000)
        for latitude, longitude, radius in (
            (float("nan"), 0, 10),
            (91, 0, 10),
            (0, 181, 10),
            (0, 0, float("inf")),
            (0, 0, 0),
        ):
            with self.subTest(
                latitude=latitude, longitude=longitude, radius=radius
            ):
                with self.assertRaises(ValueError):
                    client.GET_point(radius, lat=latitude, lon=longitude)

    def test_closest_uses_explicit_location_and_marks_source(self):
        now = 1_800_000_000
        client = ADSB_LOL()
        client._external_get = Mock(return_value=self.response({
            "now": now * 1000,
            "ac": [{
                "hex": "abc123", "flight": "CLOSE1", "lat": 34.01,
                "lon": -118.01, "seen": 0, "seen_pos": 0,
            }],
        }))
        with patch("adsblol.time.time", return_value=now):
            aircraft = client.GET_closest(50, lat=34.0, lon=-118.0)
        self.assertEqual(len(aircraft), 1)
        self.assertEqual(aircraft[0]["source"], "remote_closest")
        self.assertLess(aircraft[0]["range"], 1)
        self.assertIn(
            "/v2/closest/34.0/-118.0/50",
            client._external_get.call_args.args[1],
        )

    def test_closest_response_is_not_locally_radius_filtered(self):
        now = 1_800_000_000
        client = ADSB_LOL()
        client._external_get = Mock(return_value=self.response({
            "now": now,
            "ac": [{
                "hex": "far123", "lat": 35.0, "lon": -118.0,
                "seen": 0, "seen_pos": 0,
            }],
        }))
        with patch("adsblol.time.time", return_value=now):
            aircraft = client.GET_closest(1, lat=34.0, lon=-118.0)
        self.assertEqual(aircraft[0]["hex"], "far123")
        self.assertGreater(aircraft[0]["range"], 1)

    def test_remote_message_age_is_enforced_independently(self):
        now = 1_800_000_000
        client = ADSB_LOL(lat=33.5, lon=-117.1)
        client._external_get = Mock(return_value=self.response({
            "now": now,
            "ac": [{
                "hex": "abc123",
                "lat": 33.5,
                "lon": -117.1,
                "seen": 600,
                "seen_pos": 1,
            }],
        }))
        with patch("adsblol.time.time", return_value=now):
            self.assertEqual(client.GET_point(50, provider="public"), [])

    def test_endpoint_selection_never_implicitly_enables_remote(self):
        self.assertEqual(
            RemoteAircraftPoller.select_provider("auto", True, True),
            "feeder",
        )
        self.assertEqual(
            RemoteAircraftPoller.select_provider("auto", True, False),
            "public",
        )
        self.assertEqual(
            RemoteAircraftPoller.select_provider("feeder", False, False),
            "feeder",
        )

    def test_slow_remote_request_does_not_block_local_work_and_obsolete_result_drops(self):
        started = Event()
        release = Event()
        client = Mock()

        def slow(*_args, **_kwargs):
            started.set()
            release.wait()
            return [formatted_aircraft(100, source="remote_public")]

        client.GET_point.side_effect = slow
        clock = [10.0]
        wall = [100.0]
        poller = RemoteAircraftPoller(
            client,
            monotonic_clock=lambda: clock[0],
            wall_clock=lambda: wall[0],
        )
        try:
            poller.configure(
                enabled=True, provider="public", latitude=1, longitude=2, radius=50
            )
            self.assertEqual(poller.tick(), [])
            self.assertTrue(started.wait(1))
            local_cycles = 0
            for _ in range(5):
                local_cycles += 1
                self.assertEqual(poller.tick(), [])
            self.assertEqual(local_cycles, 5)
            poller.configure(
                enabled=False, provider="public", latitude=1, longitude=2, radius=50
            )
            release.set()
            wait_for(lambda: poller.future.done())
            self.assertEqual(poller.tick(), [])
            self.assertIsNone(poller.diagnostics()["last_success_at"])
        finally:
            release.set()
            poller.shutdown()

    def test_rate_limit_and_authorization_use_bounded_backoff(self):
        for status, expected_state, minimum in (
            (429, "rate_limited", REMOTE_RETRY_MIN_SECONDS),
            (403, "unauthorized", 300),
        ):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                if status == 429:
                    response.headers["Retry-After"] = "bad"
                error = requests.HTTPError("denied", response=response)
                client = Mock()
                client.GET_point.side_effect = error
                clock = [10.0]
                wall = [100.0]
                poller = RemoteAircraftPoller(
                    client,
                    monotonic_clock=lambda: clock[0],
                    wall_clock=lambda: wall[0],
                )
                try:
                    poller.configure(
                        enabled=True, provider="feeder",
                        latitude=1, longitude=2, radius=50,
                    )
                    poller.tick()
                    wait_for(lambda: poller.future.done())
                    poller.tick()
                    diagnostics = poller.diagnostics()
                    self.assertEqual(diagnostics["status"], expected_state)
                    self.assertGreaterEqual(
                        diagnostics["next_retry_at"] - wall[0], minimum
                    )
                    clock[0] += minimum - 1
                    poller.tick()
                    self.assertEqual(client.GET_point.call_count, 1)
                finally:
                    poller.shutdown()

    def test_cached_remote_snapshot_expires_after_failure(self):
        client = Mock()
        client.GET_point.side_effect = [
            [formatted_aircraft(100, source="remote_public")],
            requests.ConnectionError("offline"),
        ]
        clock = [10.0]
        wall = [100.0]
        poller = RemoteAircraftPoller(
            client,
            monotonic_clock=lambda: clock[0],
            wall_clock=lambda: wall[0],
        )
        try:
            poller.configure(
                enabled=True, provider="public", latitude=1, longitude=2, radius=50
            )
            poller.tick()
            wait_for(lambda: poller.future.done())
            self.assertEqual(len(poller.tick()), 1)

            clock[0] += 10
            wall[0] += 10
            poller.tick()
            wait_for(lambda: poller.future.done())
            self.assertEqual(len(poller.tick()), 1)

            clock[0] += 31
            wall[0] += 31
            self.assertEqual(poller.tick(), [])
        finally:
            poller.shutdown()

    def test_invalid_remote_schema_is_a_failure_not_an_empty_snapshot(self):
        client = ADSB_LOL()
        response = self.response({"now": time.time(), "unexpected": []})
        client._external_get = Mock(return_value=response)
        with self.assertRaises(ValueError):
            client.GET_point(50, provider="feeder")

    def test_feeder_failure_never_falls_back_to_public(self):
        client = ADSB_LOL()
        response = requests.Response()
        response.status_code = 403
        client._external_get = Mock(
            side_effect=requests.HTTPError("unauthorized", response=response)
        )
        with self.assertRaises(requests.HTTPError):
            client.GET_point(50, provider="feeder")
        self.assertEqual(client._external_get.call_count, 1)
        self.assertEqual(client._external_get.call_args.args[0], "adsblol_re_api")

    def test_upload_relay_uses_only_configured_local_beast_source(self):
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "local_receiver.shutil.which",
                    return_value="/usr/local/bin/readsb",
                ),
                patch(
                    "local_receiver.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                feeder = ADSBLolFeeder(directory)
                feeder.sync(True, "/run/readsb/aircraft.json", 30005)
                command = popen.call_args.args[0]
                feeder.stop()
        self.assertIn("--net-connector=127.0.0.1,30005,beast_in", command)
        self.assertTrue(any("feed.adsb.lol,30004" in part for part in command))
        self.assertFalse(any("api.adsb.lol" in part for part in command))
        self.assertFalse(any("re-api.adsb.lol" in part for part in command))


class ReadPathFreshnessTests(unittest.TestCase):
    def test_fallback_diagnostics_persist_to_headers_and_both_apis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostics.db"
            database = Database(path)
            now = time.time()

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                client = web_app.app.test_client()
                for status in (
                    "requesting", "remote_fallback", "stale",
                    "rate_limited", "unauthorized", "error", "local",
                    "disabled",
                ):
                    with self.subTest(status=status):
                        database.update_collector_status(
                            heartbeat_at=now,
                            acquisition_policy="local_with_fallback",
                            eligible_local_position_count=0,
                            fallback_provider="public",
                            fallback_status=status,
                            fallback_in_flight=int(status == "requesting"),
                            fallback_last_attempt_at=now - 1,
                            fallback_last_success_at=now - 2,
                            fallback_next_request_at=now + 15,
                            fallback_http_status=429 if status == "rate_limited" else 200,
                            fallback_error="test error" if status == "error" else None,
                            fallback_request_count=7,
                        )
                        aircraft = client.get("/api/aircraft")
                        nearest = client.get("/api/aircraft/nearest").get_json()
                        diagnostics = client.get("/api/diagnostics").get_json()

                        self.assertEqual(
                            aircraft.headers["X-Acquisition-State"], status
                        )
                        self.assertEqual(nearest["fallback_status"], status)
                        self.assertEqual(
                            diagnostics["acquisition"]["fallback"]["status"],
                            status,
                        )
                        self.assertEqual(
                            diagnostics["acquisition"]["fallback"]["request_count"],
                            7,
                        )

    def test_existing_collector_status_table_gets_every_supported_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.db"
            with connect_database(path) as conn:
                conn.execute("""
                    CREATE TABLE collector_status (
                        id INTEGER PRIMARY KEY,
                        heartbeat_at REAL
                    )
                """)
                conn.execute("INSERT INTO collector_status VALUES (1, NULL)")

            Database(path)
            with connect_database(path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(collector_status)"
                    ).fetchall()
                }

        self.assertTrue(COLLECTOR_STATUS_FIELDS.issubset(columns))

    def test_stopped_collector_cannot_serve_live_aircraft_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            now = time.time()
            database = Database(path)
            database.update_ac([formatted_aircraft(now)])
            database.update_collector_status(heartbeat_at=now - 100)

            def isolated_connection(*, rows=False):
                return connect_database(path, rows=rows)

            with patch.object(web_app, "connect_database", isolated_connection):
                response = web_app.app.test_client().get("/api/aircraft")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])
        self.assertEqual(response.headers["X-Collector-Fresh"], "0")

    def test_fresh_empty_local_snapshot_clears_live_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            now = time.time()
            database = Database(path)
            database.update_ac([formatted_aircraft(now)])
            database.update_ac([], authoritative_sources={"local"})
            with database.connect() as conn:
                row = conn.execute(
                    "SELECT lat, position_observed_at FROM adsb_data"
                ).fetchone()
        self.assertEqual(tuple(row), (None, None))


if __name__ == "__main__":
    unittest.main()

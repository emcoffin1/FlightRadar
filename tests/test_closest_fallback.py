import time
import unittest
from threading import Event
from unittest.mock import Mock

import requests

from closest_fallback import ClosestAircraftFallback


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


class ClosestAircraftFallbackTests(unittest.TestCase):
    def make_fallback(self, client, monotonic_now, wall_now, **configuration):
        fallback = ClosestAircraftFallback(
            client,
            monotonic_clock=lambda: monotonic_now[0],
            wall_clock=lambda: wall_now[0],
        )
        self.addCleanup(fallback.shutdown)
        fallback.configure(
            enabled=True,
            provider="public",
            latitude=33.5,
            longitude=-117.1,
            radius=50,
            normal_enabled=True,
            military_enabled=True,
            **configuration,
        )
        return fallback

    def test_requests_at_most_once_per_fifteen_seconds(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        client = Mock()
        client.GET_closest.return_value = [{
            "hex": "abc123",
            "source": "remote_closest",
            "position_observed_at": wall_now[0],
        }]
        fallback = self.make_fallback(client, monotonic_now, wall_now)

        self.assertEqual(
            fallback.tick(active=True),
            [],
        )
        wait_for(lambda: client.GET_closest.call_count == 1)
        snapshot = fallback.tick(active=True)
        self.assertEqual(snapshot[0]["hex"], "abc123")

        monotonic_now[0] = 114.9
        fallback.tick(active=True)
        self.assertEqual(client.GET_closest.call_count, 1)

        monotonic_now[0] = 115.0
        fallback.tick(active=True)
        wait_for(lambda: client.GET_closest.call_count == 2)
        client.GET_closest.assert_called_with(50, lat=33.5, lon=-117.1)

    def test_fresh_cache_survives_refresh_and_then_expires(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        release = Event()
        client = Mock()
        responses = [[{
            "hex": "abc123", "position_observed_at": wall_now[0],
        }]]

        def fetch(*_args, **_kwargs):
            if responses:
                return responses.pop(0)
            release.wait(1)
            return []

        client.GET_closest.side_effect = fetch
        fallback = self.make_fallback(client, monotonic_now, wall_now)
        fallback.tick(active=True)
        wait_for(lambda: client.GET_closest.call_count == 1)
        self.assertEqual(fallback.tick(active=True)[0]["hex"], "abc123")

        monotonic_now[0] = 115
        wall_now[0] += 15
        self.assertEqual(fallback.tick(active=True)[0]["hex"], "abc123")
        wait_for(lambda: client.GET_closest.call_count == 2)
        wall_now[0] += 6
        self.assertEqual(fallback.tick(active=True), [])
        release.set()

    def test_local_recovery_rejects_late_result(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        release = Event()
        client = Mock()

        def fetch(*_args, **_kwargs):
            release.wait(1)
            return [{"hex": "late01", "position_observed_at": wall_now[0]}]

        client.GET_closest.side_effect = fetch
        fallback = self.make_fallback(client, monotonic_now, wall_now)
        fallback.tick(active=True)
        wait_for(lambda: client.GET_closest.call_count == 1)
        self.assertEqual(fallback.tick(active=False), [])
        release.set()
        wait_for(lambda: fallback.future.done())
        self.assertEqual(fallback.tick(active=False), [])
        self.assertEqual(fallback.diagnostics()["status"], "local")

    def test_filtered_query_selects_nearest_deterministically(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        client = Mock()
        client.GET_point.return_value = [
            {"hex": "bbb", "range": 2, "flag": [], "position_observed_at": wall_now[0]},
            {"hex": "aaa", "range": 2, "flag": [], "position_observed_at": wall_now[0]},
            {"hex": "mil", "range": 1, "flag": ["Military"], "position_observed_at": wall_now[0]},
        ]
        fallback = ClosestAircraftFallback(
            client,
            monotonic_clock=lambda: monotonic_now[0],
            wall_clock=lambda: wall_now[0],
        )
        self.addCleanup(fallback.shutdown)
        fallback.configure(
            enabled=True, provider="public", latitude=33.5, longitude=-117.1,
            radius=50, normal_enabled=True, military_enabled=False,
        )
        fallback.tick(active=True)
        wait_for(lambda: client.GET_point.call_count == 1)
        self.assertEqual(fallback.tick(active=True)[0]["hex"], "aaa")
        client.GET_closest.assert_not_called()

    def test_rate_limit_and_offline_permission_state(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        response = Mock(status_code=429, headers={"Retry-After": "120"})
        error = requests.HTTPError("limited", response=response)
        client = Mock()
        client.GET_closest.side_effect = error
        fallback = self.make_fallback(client, monotonic_now, wall_now)
        fallback.tick(active=True)
        wait_for(lambda: client.GET_closest.call_count == 1)
        fallback.tick(active=True)
        diagnostics = fallback.diagnostics()
        self.assertEqual(diagnostics["status"], "rate_limited")
        self.assertGreaterEqual(diagnostics["next_request_at"] - wall_now[0], 120)

        fallback.configure(
            enabled=False, provider="public", latitude=33.5, longitude=-117.1,
            radius=50, normal_enabled=True, military_enabled=True,
        )
        fallback.tick(active=True)
        self.assertEqual(client.GET_closest.call_count, 1)
        self.assertEqual(fallback.diagnostics()["status"], "disabled")

    def test_empty_parse_timeout_and_authorization_states(self):
        cases = [
            ([], "empty"),
            (ValueError("malformed payload"), "error"),
            (requests.Timeout("timed out"), "error"),
            (
                requests.HTTPError(
                    "denied",
                    response=Mock(status_code=401, headers={}),
                ),
                "unauthorized",
            ),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected, result=type(result).__name__):
                monotonic_now = [100.0]
                wall_now = [1_800_000_000.0]
                client = Mock()
                if isinstance(result, Exception):
                    client.GET_closest.side_effect = result
                else:
                    client.GET_closest.return_value = result
                fallback = self.make_fallback(client, monotonic_now, wall_now)
                fallback.tick(active=True)
                wait_for(lambda: client.GET_closest.call_count == 1)
                fallback.tick(active=True)
                self.assertEqual(fallback.diagnostics()["status"], expected)


if __name__ == "__main__":
    unittest.main()

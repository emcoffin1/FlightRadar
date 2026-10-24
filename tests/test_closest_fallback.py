import time
import unittest
from unittest.mock import Mock

from closest_fallback import ClosestAircraftFallback


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


class ClosestAircraftFallbackTests(unittest.TestCase):
    def test_requests_at_most_once_per_fifteen_seconds(self):
        monotonic_now = [100.0]
        wall_now = [1_800_000_000.0]
        client = Mock()
        client.GET_closest.return_value = [{
            "hex": "abc123",
            "source": "remote_closest",
            "position_observed_at": wall_now[0],
        }]
        fallback = ClosestAircraftFallback(
            client,
            monotonic_clock=lambda: monotonic_now[0],
            wall_clock=lambda: wall_now[0],
        )
        self.addCleanup(fallback.shutdown)

        self.assertEqual(
            fallback.tick(latitude=33.5, longitude=-117.1, radius=50),
            [],
        )
        wait_for(lambda: client.GET_closest.call_count == 1)
        snapshot = fallback.tick(latitude=33.5, longitude=-117.1, radius=50)
        self.assertEqual(snapshot[0]["hex"], "abc123")

        monotonic_now[0] = 114.9
        fallback.tick(latitude=33.5, longitude=-117.1, radius=50)
        self.assertEqual(client.GET_closest.call_count, 1)

        monotonic_now[0] = 115.0
        fallback.tick(latitude=33.5, longitude=-117.1, radius=50)
        wait_for(lambda: client.GET_closest.call_count == 2)
        client.GET_closest.assert_called_with(50, lat=33.5, lon=-117.1)


if __name__ == "__main__":
    unittest.main()

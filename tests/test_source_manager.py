import unittest

from source_manager import (
    LOCAL_ACTIVE,
    LOCAL_EMPTY_PENDING,
    LOCAL_UNHEALTHY,
    OFFLINE,
    REMOTE_ACTIVE,
    REMOTE_EMPTY,
    REMOTE_UNAVAILABLE,
    SourceManager,
)


class SourceManagerTests(unittest.TestCase):
    def decide(self, manager, **overrides):
        settings = {
            "local_enabled": True,
            "local_snapshot_valid": True,
            "eligible_local_count": 0,
            "fallback_enabled": True,
            "continuous_remote": False,
            "offline": False,
        }
        settings.update(overrides)
        return manager.decide(**settings)

    def test_three_valid_empty_snapshots_activate_fallback(self):
        manager = SourceManager()
        for _ in range(2):
            decision = self.decide(manager)
            self.assertFalse(decision["poll_fallback"])
            self.assertEqual(manager.state, LOCAL_EMPTY_PENDING)
        decision = self.decide(manager)
        self.assertTrue(decision["poll_fallback"])

    def test_fallback_requires_explicit_consent(self):
        manager = SourceManager()
        for _ in range(5):
            decision = self.decide(manager, fallback_enabled=False)
        self.assertFalse(decision["poll_fallback"])
        self.assertFalse(manager.fallback_active)

    def test_local_recovery_is_immediate_but_stops_remote_after_two(self):
        manager = SourceManager()
        for _ in range(3):
            self.decide(manager)
        first = self.decide(manager, eligible_local_count=1)
        self.assertEqual(manager.state, LOCAL_ACTIVE)
        self.assertTrue(first["poll_fallback"])
        second = self.decide(manager, eligible_local_count=1)
        self.assertFalse(second["poll_fallback"])

    def test_receiver_failure_and_remote_outcomes_are_distinct(self):
        manager = SourceManager()
        for _ in range(3):
            self.decide(manager, local_snapshot_valid=False)
        self.assertEqual(manager.state, LOCAL_UNHEALTHY)
        manager.observe_remote("empty", False)
        self.assertEqual(manager.state, REMOTE_EMPTY)
        manager.observe_remote("unauthorized", False)
        self.assertEqual(manager.state, REMOTE_UNAVAILABLE)
        manager.observe_remote("ready", True)
        self.assertEqual(manager.state, REMOTE_ACTIVE)

    def test_offline_never_polls(self):
        manager = SourceManager()
        decision = self.decide(manager, offline=True)
        self.assertEqual(manager.state, OFFLINE)
        self.assertFalse(decision["poll_fallback"])
        self.assertFalse(decision["display_remote"])


if __name__ == "__main__":
    unittest.main()

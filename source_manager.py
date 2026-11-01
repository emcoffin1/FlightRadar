"""Small source-selection state machine for local-first ADS-B acquisition."""

LOCAL_ACTIVE = "LOCAL_ACTIVE"
LOCAL_EMPTY_PENDING = "LOCAL_EMPTY_PENDING"
REMOTE_ACTIVE = "REMOTE_ACTIVE"
REMOTE_EMPTY = "REMOTE_EMPTY"
REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
LOCAL_UNHEALTHY = "LOCAL_UNHEALTHY"
OFFLINE = "OFFLINE"


class SourceManager:
    """Apply local-empty and local-recovery hysteresis without doing I/O."""

    def __init__(self, *, empty_threshold=3, recovery_threshold=2):
        self.empty_threshold = int(empty_threshold)
        self.recovery_threshold = int(recovery_threshold)
        self.empty_streak = 0
        self.failure_streak = 0
        self.recovery_streak = 0
        self.fallback_active = False
        self.state = LOCAL_UNHEALTHY
        self._configuration = None

    def decide(
        self,
        *,
        local_enabled,
        local_snapshot_valid,
        eligible_local_count,
        fallback_enabled,
        continuous_remote,
        offline,
    ):
        configuration = (
            bool(local_enabled), bool(fallback_enabled),
            bool(continuous_remote), bool(offline),
        )
        if self._configuration != configuration:
            self._configuration = configuration
            self.empty_streak = 0
            self.failure_streak = 0
            self.recovery_streak = 0
            self.fallback_active = False

        if offline:
            self.state = OFFLINE
            self.fallback_active = False
            return self._decision(False, False)

        local_present = bool(local_snapshot_valid and eligible_local_count > 0)
        if local_present:
            self.empty_streak = 0
            self.failure_streak = 0
            self.recovery_streak += 1
            poll_fallback = self.fallback_active
            if self.recovery_streak >= self.recovery_threshold:
                self.fallback_active = False
                poll_fallback = False
            self.state = LOCAL_ACTIVE
            return self._decision(poll_fallback, False)

        self.recovery_streak = 0
        if local_snapshot_valid:
            self.failure_streak = 0
            self.empty_streak += 1
            if self.empty_streak >= self.empty_threshold and fallback_enabled:
                self.fallback_active = True
            self.state = LOCAL_EMPTY_PENDING
        else:
            self.empty_streak = 0
            self.failure_streak += 1
            if (
                fallback_enabled
                and (not local_enabled or self.failure_streak >= self.empty_threshold)
            ):
                self.fallback_active = True
            self.state = LOCAL_UNHEALTHY

        if continuous_remote:
            return self._decision(False, True)
        return self._decision(self.fallback_active, self.fallback_active)

    def observe_remote(self, status, has_fresh_aircraft):
        if self.state in {LOCAL_ACTIVE, OFFLINE}:
            return self.state
        if has_fresh_aircraft:
            self.state = REMOTE_ACTIVE
        elif status == "empty":
            self.state = REMOTE_EMPTY
        elif status in {
            "unauthorized", "rate_limited", "error", "stale",
        }:
            self.state = REMOTE_UNAVAILABLE
        return self.state

    def diagnostics(self):
        return {
            "state": self.state,
            "empty_streak": self.empty_streak,
            "failure_streak": self.failure_streak,
            "recovery_streak": self.recovery_streak,
            "fallback_active": self.fallback_active,
        }

    @staticmethod
    def _decision(poll_fallback, display_remote):
        return {
            "poll_fallback": bool(poll_fallback),
            "display_remote": bool(display_remote),
        }

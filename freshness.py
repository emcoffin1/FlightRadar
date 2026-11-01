"""Shared ADS-B timestamp validation and freshness semantics."""

import math
import time

from config import CLOCK_SKEW_TOLERANCE_SECONDS


def finite_number(value):
    """Return a finite float, otherwise ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def provider_snapshot_seconds(value, provider):
    """Normalize a provider timestamp at the network boundary.

    Public adsb.lol v2 uses Unix milliseconds. The feeder re-api exposes
    readsb JSON, which normally uses Unix seconds. Accept either magnitude for
    both remote providers so a provider transition cannot silently make every
    observation invalid, while keeping local readsb processing unchanged.
    """
    timestamp = finite_number(value)
    if timestamp is None or timestamp <= 0:
        return None
    if provider not in {"public", "closest", "feeder"}:
        raise ValueError("unknown timestamp provider")
    if timestamp >= 100_000_000_000:
        timestamp /= 1000
    return timestamp


def valid_absolute_time(value, *, now=None, future_tolerance=None):
    """Validate an absolute UTC Unix timestamp without silently replacing it."""
    timestamp = finite_number(value)
    if timestamp is None or timestamp <= 0:
        return None
    current = time.time() if now is None else float(now)
    tolerance = (
        CLOCK_SKEW_TOLERANCE_SECONDS
        if future_tolerance is None
        else float(future_tolerance)
    )
    if timestamp > current + tolerance:
        return None
    return timestamp


def observation_time(snapshot_time, age, *, fetched_at=None):
    """Convert readsb's relative age to an absolute observation timestamp."""
    snapshot = valid_absolute_time(snapshot_time, now=fetched_at)
    relative_age = finite_number(age)
    if snapshot is None or relative_age is None or relative_age < 0:
        return None
    observed = snapshot - relative_age
    if observed <= 0:
        return None
    if fetched_at is not None and observed > float(fetched_at) + CLOCK_SKEW_TOLERANCE_SECONDS:
        return None
    return observed


def is_fresh(timestamp, max_age, *, now=None):
    current = time.time() if now is None else float(now)
    observed = valid_absolute_time(timestamp, now=current)
    return observed is not None and current - observed <= float(max_age)

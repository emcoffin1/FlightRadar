"""Defensive parsing for HTTP Retry-After response headers."""

from email.utils import parsedate_to_datetime
import time


def parse_retry_after(value, now=None):
    """Return a positive delay in seconds, or ``None`` for unusable guidance.

    Retry-After permits either delay-seconds or an HTTP date.  Dates are
    converted to a duration using the wall clock so callers can safely add the
    result to a monotonic deadline.
    """
    if value is None:
        return None

    value = str(value).strip()
    if not value:
        return None

    try:
        delay = int(value, 10)
    except (TypeError, ValueError, OverflowError):
        try:
            current = time.time() if now is None else float(now)
            delay = int(parsedate_to_datetime(value).timestamp() - current)
        except (TypeError, ValueError, OverflowError):
            return None

    return delay if delay > 0 else None


def retry_after_seconds(error, now=None):
    """Read positive Retry-After guidance from a requests-style exception."""
    try:
        response = getattr(error, "response", None)
        value = response.headers.get("Retry-After") if response is not None else None
        return parse_retry_after(value, now=now)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None

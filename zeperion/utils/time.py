"""Time helpers."""

from datetime import datetime, timezone


def iso_now() -> str:
    """Return the current UTC time as a timezone-aware ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def utc_strftime(fmt: str) -> str:
    """Return the current UTC time formatted with ``fmt`` (timezone-aware)."""
    return datetime.now(timezone.utc).strftime(fmt)

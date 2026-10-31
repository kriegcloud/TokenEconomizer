"""Small history-discovery helpers shared by hosts, without a dependency on plugin state.

Discovery reads metadata only. Callers choose the cutoff as a Unix timestamp; timestamps in
agent logs are ISO 8601, with UTC assumed when an older log leaves the zone off.
"""
from datetime import datetime, timezone


def timestamp(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None

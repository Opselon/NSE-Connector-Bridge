"""UTC/broker-time helpers.

MT5 epoch seconds are broker-server-local seconds since 1970. The official
package converts datetime arguments assuming UTC; the broker offset is
applied symmetrically at the boundaries (ported from the production adapter,
BUG-188: copy_ticks_range resolves boundaries in server-local time).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .config import BridgeSettings


def broker_epoch_to_utc(epoch_s: float, offset_minutes: int = 0) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=UTC) - timedelta(minutes=offset_minutes)


def normalize_utc(value: Any, offset_minutes: int = 0) -> datetime | None:
    """Coerce int epoch / ISO string / datetime into aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    elif isinstance(value, (int, float)):
        # treat > 1e12 as milliseconds
        if value > 1e12:
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(float(value), tz=UTC) - timedelta(minutes=offset_minutes)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    return dt


def broker_dt(value: Any, settings: BridgeSettings) -> datetime | None:
    """Parse an incoming boundary and shift it into the broker timebase."""
    dt = normalize_utc(value, settings.broker_utc_offset_minutes)
    if dt is None:
        return None
    return dt + timedelta(minutes=settings.broker_utc_offset_minutes)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None

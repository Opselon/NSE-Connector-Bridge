"""Unit tests for UTC / broker-time helpers (nse_bridge.timeutil).

MT5 epoch seconds are broker-server-local seconds since 1970; the broker
offset is applied symmetrically at the boundaries (BUG-188).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.timeutil import broker_dt, broker_epoch_to_utc, iso, normalize_utc

pytestmark = pytest.mark.unit


def test_broker_epoch_to_utc_zero_offset():
    assert broker_epoch_to_utc(0, 0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert broker_epoch_to_utc(1_700_000_000, 0) == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=UTC
    )


def test_broker_epoch_to_utc_applies_offset():
    # A broker 3h ahead of UTC: its epoch 0 is 1969-12-31 21:00 UTC.
    assert broker_epoch_to_utc(0, 180) == datetime(1969, 12, 31, 21, 0, tzinfo=UTC)


def test_normalize_utc_epoch_seconds_and_milliseconds():
    assert normalize_utc(0) == datetime(1970, 1, 1, tzinfo=UTC)
    # values > 1e12 are treated as milliseconds
    assert normalize_utc(1_700_000_000_000) == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=UTC
    )


def test_normalize_utc_iso_string_with_z():
    assert normalize_utc("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    # An explicit offset is kept; the instant is 00:00 UTC.
    parsed = normalize_utc("2026-01-01T03:00:00+03:00")
    assert parsed is not None
    assert parsed.utctimetuple().tm_hour == 0
    assert parsed == datetime(2026, 1, 1, tzinfo=UTC)


def test_normalize_utc_naive_datetime_assumed_utc():
    assert normalize_utc(datetime(2026, 5, 1, 12, 0)) == datetime(
        2026, 5, 1, 12, 0, tzinfo=UTC
    )


def test_normalize_utc_invalid_inputs_return_none():
    assert normalize_utc(None) is None
    assert normalize_utc("") is None
    assert normalize_utc("   ") is None
    assert normalize_utc("not-a-date") is None
    assert normalize_utc(10**20) is None  # far-out-of-range epoch


def test_normalize_utc_applies_offset():
    assert normalize_utc(0, offset_minutes=120) == datetime(
        1969, 12, 31, 22, 0, tzinfo=UTC
    )


def test_broker_dt_round_trips_through_settings_offset():
    settings = BridgeSettings(broker_utc_offset_minutes=120, _env_file=None)
    # broker_dt shifts the boundary into the broker timebase: for a
    # broker 2h ahead, epoch 0 lands back at 1970-01-01 00:00 UTC.
    assert broker_dt(0, settings) == datetime(1970, 1, 1, tzinfo=UTC)
    zero_offset = BridgeSettings(broker_utc_offset_minutes=0, _env_file=None)
    assert broker_dt(0, zero_offset) == datetime(1970, 1, 1, tzinfo=UTC)


def test_broker_dt_invalid_returns_none():
    settings = BridgeSettings(_env_file=None)
    assert broker_dt("garbage", settings) is None
    assert broker_dt(None, settings) is None


def test_iso_helper():
    assert iso(None) is None
    assert iso(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"

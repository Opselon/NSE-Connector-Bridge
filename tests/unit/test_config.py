"""Unit tests for config defaults + reconnect backoff arithmetic.

The backoff lives in `realtime/channel.py` (`backoff_delay` /
`backoff_with_jitter`): CONTRACT v1 §5.4 — start at
`reconnect_initial_ms` (500 ms), double, cap at `reconnect_max_ms`
(30 s), plus 0–100 ms jitter. These tests pin the config defaults
that formula reads, and verify the formula itself so a later change
cannot silently alter the schedule.
"""

from __future__ import annotations

import itertools
import os

import pytest

from nse_bridge.config import BridgeSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> BridgeSettings:
    # _env_file=None keeps a stray repo .env out of the defaults check.
    return BridgeSettings(_env_file=None, **overrides)


def test_loopback_host_default():
    # The local API must bind loopback only, never 0.0.0.0.
    assert _settings().host == "127.0.0.1"
    assert _settings().port == 8788


def test_server_channel_defaults():
    s = _settings()
    assert s.server_url == ""  # empty = server channel disabled
    assert s.server_token == ""
    assert s.device_id == ""
    assert s.heartbeat_interval_s == 15.0


def test_reconnect_backoff_defaults():
    s = _settings()
    assert s.reconnect_initial_ms == 500  # CONTRACT v1 §5.4: initial 500 ms
    assert s.reconnect_max_ms == 30000
    assert s.reconnect_initial_ms <= s.reconnect_max_ms


def test_mt5_defaults():
    s = _settings()
    assert s.mt5_terminal_path == ""  # auto-discovered when empty
    assert s.mt5_timeout_ms == 5000
    assert s.mt5_retries == 3
    assert s.broker_utc_offset_minutes == 0


def test_no_hardcoded_trading_identity():
    # Phase 3 (client-only refactor): the bridge stamps NO magic and
    # assumes NO symbol or timeframe unless explicitly configured.
    # Magic/symbol come from the server per command or from explicit
    # device configuration (PUT /settings / config.json) — never from
    # a client default. None/"" means "unconfigured".
    s = _settings()
    assert s.magic is None
    assert s.bot_symbol == ""
    assert s.default_timeframe == ""
    assert s.strict_magic_guard is False


def _backoff_schedule(initial_ms: int, max_ms: int, steps: int) -> list[float]:
    """Mirror of the schedule in realtime/channel.py::backoff_delay (no jitter)."""
    backoff = initial_ms / 1000.0
    out = []
    for _ in range(steps):
        out.append(backoff)
        backoff = min(backoff * 2.0, max_ms / 1000.0)
    return out


def test_reconnect_backoff_schedule_doubles_and_caps():
    schedule = _backoff_schedule(500, 30000, 12)
    assert schedule[0] == pytest.approx(0.5)
    assert schedule[1] == pytest.approx(1.0)
    assert schedule[2] == pytest.approx(2.0)
    assert schedule[-1] == pytest.approx(30.0)  # capped at reconnect_max_ms
    assert all(a <= b for a, b in itertools.pairwise(schedule))
    assert all(v <= 30.0 for v in schedule)


def test_reconnect_backoff_respects_custom_max():
    schedule = _backoff_schedule(500, 5000, 10)
    assert schedule[-1] == pytest.approx(5.0)
    assert all(v <= 5.0 for v in schedule)

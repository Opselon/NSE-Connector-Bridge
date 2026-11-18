"""Unit tests for the chart protocol (CONTRACT v1 §17.1).

Label: unit. No MT5 — fetchers are fakes; shapes, limits, error
codes, deltas and the rate budget are pure.
"""

from __future__ import annotations

import pytest

from nse_bridge.server.chart import (
    CHART_MAX_COUNT,
    CHART_MAX_SUBSCRIPTIONS,
    ChartError,
    ChartErrorCode,
    ChartRateLimiter,
    ChartService,
    build_chart_data_frame,
    build_chart_error_frame,
    build_chart_tick_frame,
    validate_chart_request,
)
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit


def _candles(n=3):
    return [
        {
            "time": f"2026-11-16T00:{15 * i:02d}:00",
            "open": 1.10,
            "high": 1.11,
            "low": 1.09,
            "close": 1.105,
            "tick_volume": 100 + i,
        }
        for i in range(n)
    ]


def _service(**overrides):
    calls: dict[str, int] = {"candles": 0}

    def fetch_candles(symbol, timeframe, count, from_, to):
        calls["candles"] += 1
        if symbol == "UNKNOWN":
            raise ChartError(ChartErrorCode.SYMBOL_NOT_FOUND, "no such symbol")
        return _candles(min(count, 3))

    def fetch_ticks(symbol, count, from_, to):
        return [{"time_ms": "2026-11-16T19:53:00.034", "bid": 1.10042, "ask": 1.10043}]

    def ensure_market_watch(symbol):
        if symbol == "UNKNOWN":
            raise ChartError(ChartErrorCode.SYMBOL_NOT_FOUND, "no such symbol")

    kwargs = {
        "fetch_candles": fetch_candles,
        "fetch_ticks": fetch_ticks,
        "ensure_market_watch": ensure_market_watch,
    }
    kwargs.update(overrides)
    service = ChartService(**kwargs)
    service._calls = calls  # type: ignore[attr-defined]
    return service


@pytest.mark.unit
def test_validate_chart_request_ok_and_normalized():
    assert validate_chart_request("eurusd", "m15", 100) == ("EURUSD", "M15", 100)


@pytest.mark.unit
def test_validate_chart_request_bad_timeframe():
    with pytest.raises(ChartError) as excinfo:
        validate_chart_request("EURUSD", "M99", 100)
    assert excinfo.value.code == ChartErrorCode.UNSUPPORTED_TIMEFRAME


@pytest.mark.unit
def test_validate_chart_request_count_clamped_to_1000():
    # §17.1.3 batching limit; the server pages larger ranges itself.
    assert validate_chart_request("EURUSD", "M15", 5000)[2] == CHART_MAX_COUNT
    assert validate_chart_request("EURUSD", "M15", 0)[2] == 1


@pytest.mark.unit
def test_chart_timeframes_m1_to_mn1():
    for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"):
        assert validate_chart_request("EURUSD", tf, 10)[1] == tf


@pytest.mark.unit
def test_handle_request_returns_contract_chart_data():
    service = _service()
    request_id = new_uuid7_str()
    frames = service.handle_request(
        {
            "type": "chart.request",
            "request_id": request_id,
            "symbol": "EURUSD",
            "timeframe": "M15",
            "count": 3,
        }
    )
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "chart.data"
    assert frame["request_id"] == request_id
    assert frame["subscription_id"] is None
    assert frame["symbol"] == "EURUSD"
    assert frame["complete"] is True and frame["status"] == "ok"
    assert len(frame["candles"]) == 3
    assert frame["candles"][0]["time"] == "2026-11-16T00:00:00"
    assert "received_at" in frame and "broker_time" in frame


@pytest.mark.unit
def test_handle_request_duplicate_id_answered_from_cache():
    service = _service()
    request_id = new_uuid7_str()
    frame = {"type": "chart.request", "request_id": request_id, "symbol": "EURUSD", "timeframe": "M15", "count": 3}
    service.handle_request(frame)
    service.handle_request(frame)
    assert service._calls["candles"] == 1  # never a second MT5 call (§17.1.3)


@pytest.mark.unit
def test_handle_request_unknown_symbol_error_frame():
    service = _service()
    frames = service.handle_request(
        {
            "type": "chart.request",
            "request_id": new_uuid7_str(),
            "symbol": "UNKNOWN",
            "timeframe": "M15",
            "count": 3,
        }
    )
    assert frames[0]["status"] == "error"
    assert frames[0]["error_code"] == "symbol_not_found"
    assert frames[0]["complete"] is False


@pytest.mark.unit
def test_handle_request_with_ticks_returns_tick_frame():
    service = _service()
    frames = service.handle_request(
        {
            "type": "chart.request",
            "request_id": new_uuid7_str(),
            "symbol": "EURUSD",
            "timeframe": "M15",
            "count": 3,
            "include_ticks": True,
        }
    )
    assert len(frames) == 2
    assert frames[1]["type"] == "chart.tick"
    assert frames[1]["ticks"][0]["bid"] == 1.10042


@pytest.mark.unit
def test_subscribe_limit_20_then_subscription_limit_error():
    service = _service()
    for _ in range(CHART_MAX_SUBSCRIPTIONS):
        assert (
            service.handle_subscribe(
                {
                    "type": "chart.subscribe",
                    "subscription_id": new_uuid7_str(),
                    "symbol": "EURUSD",
                    "timeframe": "M15",
                }
            )
            is None
        )
    error = service.handle_subscribe(
        {
            "type": "chart.subscribe",
            "subscription_id": new_uuid7_str(),
            "symbol": "EURUSD",
            "timeframe": "M15",
        }
    )
    assert error is not None
    assert error["error_code"] == "subscription_limit"


@pytest.mark.unit
def test_subscribe_poll_interval_clamped_5_to_3600_default_15():
    service = _service()
    service.handle_subscribe(
        {
            "type": "chart.subscribe",
            "subscription_id": "s-low",
            "symbol": "EURUSD",
            "timeframe": "M15",
            "poll_interval_s": 1,
        }
    )
    service.handle_subscribe(
        {
            "type": "chart.subscribe",
            "subscription_id": "s-high",
            "symbol": "EURUSD",
            "timeframe": "M15",
            "poll_interval_s": 9999,
        }
    )
    service.handle_subscribe(
        {
            "type": "chart.subscribe",
            "subscription_id": "s-default",
            "symbol": "EURUSD",
            "timeframe": "M15",
        }
    )
    subs = service._subscriptions
    assert subs["s-low"].poll_interval_s == 5
    assert subs["s-high"].poll_interval_s == 3600
    assert subs["s-default"].poll_interval_s == 15


@pytest.mark.unit
def test_unsubscribe_unknown_id_is_noop():
    service = _service()
    service.handle_unsubscribe({"type": "chart.unsubscribe", "subscription_id": "nope"})
    assert service.subscription_count == 0


@pytest.mark.unit
def test_build_delta_only_new_and_forming_candles():
    service = _service()
    service.handle_subscribe(
        {
            "type": "chart.subscribe",
            "subscription_id": "s1",
            "symbol": "EURUSD",
            "timeframe": "M15",
        }
    )
    sub = service._subscriptions["s1"]
    first = service.build_delta(sub, _candles(3))
    assert len(first) == 3
    # Same candles again: only the still-forming last candle is re-sent.
    again = service.build_delta(sub, _candles(3))
    assert len(again) == 1
    assert again[0]["time"] == "2026-11-16T00:30:00"
    # A new candle arrives: it plus the forming one are sent.
    extended = _candles(3) + [
        {
            "time": "2026-11-16T00:45:00",
            "open": 1.10,
            "high": 1.11,
            "low": 1.09,
            "close": 1.105,
            "tick_volume": 103,
        }
    ]
    third = service.build_delta(sub, extended)
    assert [c["time"] for c in third] == ["2026-11-16T00:30:00", "2026-11-16T00:45:00"]


@pytest.mark.unit
def test_rate_limiter_60_per_minute():
    limiter = ChartRateLimiter(limit_per_minute=60)
    now = 1000.0
    assert all(limiter.allow(now=now) for _ in range(60))
    assert limiter.allow(now=now) is False  # 61st within the minute
    assert limiter.allow(now=now + 61.0) is True  # window slid past


@pytest.mark.unit
def test_chart_error_frame_shape():
    frame = build_chart_error_frame(
        request_id="r1",
        subscription_id=None,
        symbol="EURUSD",
        timeframe="M15",
        code="market_closed",
        message="market closed",
    )
    assert frame["status"] == "error"
    assert frame["error_code"] == "market_closed"
    assert frame["request_id"] == "r1"
    assert frame["complete"] is False


@pytest.mark.unit
def test_retry_uses_a_new_request_id():
    # §17.1.3: a retry MUST NOT reuse the old request_id.
    assert new_uuid7_str() != new_uuid7_str()


@pytest.mark.unit
def test_chart_data_frame_builder_shape():
    frame = build_chart_data_frame(
        request_id="r1",
        subscription_id=None,
        symbol="EURUSD",
        timeframe="M15",
        candles=_candles(1),
        broker_time="2026-11-16T00:00:00",
    )
    assert frame["type"] == "chart.data"
    assert frame["candles"][0]["tick_volume"] == 100
    tick = build_chart_tick_frame(
        subscription_id="s1",
        request_id=None,
        symbol="EURUSD",
        ticks=[{"time_ms": "2026-11-16T19:53:00.034", "bid": 1.1, "ask": 1.11}],
        broker_time="2026-11-16T19:53:00.034",
    )
    assert tick["type"] == "chart.tick"
    assert tick["ticks"][0]["time_ms"] == "2026-11-16T19:53:00.034"

"""Category 5 — charts & market history (37-45)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import TIMEFRAME_NAMES, build_rate_bar_model, f as _f
from ..timeutil import broker_dt
from .common import BridgeContext
from .registry import command


def resolve_timeframe(ctx: BridgeContext, timeframe: str | None) -> tuple[int, str]:
    """MT5 timeframe enum + canonical name; VALIDATION error on unknown."""
    name = (timeframe or ctx.settings.default_timeframe).upper()

    def job(mt5: Any):
        return getattr(mt5, f"TIMEFRAME_{name}", None)

    tf = ctx.driver.run(job, op="timeframe_resolve")
    if tf is None:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Unknown timeframe '{timeframe}'. Supported: {', '.join(TIMEFRAME_NAMES)}",
            details={"supported": list(TIMEFRAME_NAMES)},
            http_status=422,
        )
    return int(tf), name


def validate_ohlc(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Structural OHLC validation — drops invalid rows, reports loudly (ported)."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    issues: set[str] = set()
    for b in bars:
        o, h, low, c = b["open"], b["high"], b["low"], b["close"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (o, h, low, c)):
            issues.add("non_finite")
            dropped += 1
            continue
        if min(o, c) <= 0:
            issues.add("non_positive_price")
            dropped += 1
            continue
        if h < low or h < o or h < c or low > o or low > c:
            issues.add("inverted_ohlc")
            dropped += 1
            continue
        kept.append(b)
    return {
        "kept": kept,
        "dropped": dropped,
        "issues": sorted(issues),
    }


class ChartHistoryRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    timeframe: str = Field("M1", description="M1..M30, H1..H12, D1, W1, MN1")
    count: int = Field(500, ge=1, le=100000)
    from_utc: str | None = Field(None, description="ISO/epoch; switches to range mode")


class BarsResponse(BaseModel):
    symbol: str
    timeframe: str
    requested: int
    received: int
    kept: int
    dropped: int
    issues: list[str] = []
    bars: list[dict[str, Any]]


class TicksHistoryResponse(BaseModel):
    symbol: str
    received: int
    ticks: list[dict[str, Any]]


class TimeframeInfoResponse(BaseModel):
    timeframe: str
    source: str


class AvailableTimeframesResponse(BaseModel):
    timeframes: list[str]
    enum_values: dict[str, int]


class OhlcvResponse(BaseModel):
    symbol: str
    timeframe: str
    count: int
    time: list[int]
    time_utc: list[str | None]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    tick_volume: list[int]


class CapabilitiesNoteResponse(BaseModel):
    available: bool
    items: list[str]
    note: str


class ChartActiveInfoResponse(BaseModel):
    symbol: str
    timeframe: str
    symbol_selected: bool
    last_tick_utc: str | None = None
    note: str


class MultiTimeframeResponse(BaseModel):
    symbol: str
    count: int
    series: dict[str, list[dict[str, Any]]]
    dropped_by_timeframe: dict[str, int]


@command(
    "get_chart_history",
    number=37,
    category="charts",
    title="Get Chart History",
    description="Broker candle history (copy_rates). Position mode by count, or range mode via from_utc. "
    "The still-forming current bar is included and marked; invalid OHLC rows are dropped and reported.",
    request_model=ChartHistoryRequest,
    response_model=BarsResponse,
)
def get_chart_history(ctx: BridgeContext, params: ChartHistoryRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    tf, tf_name = resolve_timeframe(ctx, params.timeframe)
    offset = ctx.settings.broker_utc_offset_minutes
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        mt5.symbol_select(symbol, True)
        if params.from_utc is not None:
            from_dt = broker_dt(params.from_utc, ctx.settings)
            if from_dt is None:
                return None
            return mt5.copy_rates_range(symbol, tf, from_dt, datetime.now(UTC))
        return mt5.copy_rates_from_pos(symbol, tf, 0, params.count)

    raw = ctx.driver.run(
        job, op="copy_rates", context={"symbol": symbol, "timeframe": tf_name, "count": params.count}
    )
    if raw is None or len(raw) == 0:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            f"No rate history for '{symbol}' ({tf_name}) — market closed or unknown symbol.",
            details={"symbol": symbol, "timeframe": tf_name},
        )
    bars = [build_rate_bar_model(r, offset).model_dump() for r in raw]
    report = validate_ohlc(bars)
    return {
        "symbol": symbol,
        "timeframe": tf_name,
        "requested": params.count if params.from_utc is None else -1,
        "received": len(bars),
        "kept": len(report["kept"]),
        "dropped": report["dropped"],
        "issues": report["issues"],
        "bars": report["kept"],
    }


class TicksHistoryRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    count: int = Field(500, ge=1, le=100000)
    from_utc: str | None = Field(None, description="Range mode start")
    to_utc: str | None = Field(None, description="Range mode end (defaults to now)")


@command(
    "get_chart_ticks_history",
    number=38,
    category="charts",
    title="Get Tick History",
    description="Tick history via copy_ticks_from (count mode) or copy_ticks_range (from/to mode).",
    request_model=TicksHistoryRequest,
    response_model=TicksHistoryResponse,
)
def get_chart_ticks_history(ctx: BridgeContext, params: TicksHistoryRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    offset = ctx.settings.broker_utc_offset_minutes
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        if params.from_utc is not None:
            from_dt = broker_dt(params.from_utc, ctx.settings)
            to_dt = broker_dt(params.to_utc, ctx.settings) or datetime.now(UTC)
            if from_dt is None or to_dt < from_dt:
                return None
            return mt5.copy_ticks_range(symbol, from_dt, to_dt, mt5.COPY_TICKS_ALL)
        return mt5.copy_ticks_from(symbol, datetime.now(UTC), params.count, mt5.COPY_TICKS_ALL)

    raw = ctx.driver.run(job, op="copy_ticks", context={"symbol": symbol, "count": params.count})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            f"No tick history for '{symbol}'.",
            details={"symbol": symbol},
        )
    from ..mt5.types import build_tick_history_model

    ticks = [build_tick_history_model(t, offset).model_dump() for t in raw]
    return {"symbol": symbol, "received": len(ticks), "ticks": ticks}


@command(
    "get_chart_current_timeframe",
    number=39,
    category="charts",
    title="Get Current Timeframe",
    description="The bridge's configured chart timeframe (MT5 API cannot read the GUI chart's timeframe).",
    response_model=TimeframeInfoResponse,
)
def get_chart_current_timeframe(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return {
        "timeframe": ctx.settings.default_timeframe,
        "source": "bridge_config",
    }


@command(
    "get_chart_available_timeframes",
    number=40,
    category="charts",
    title="Get Available Timeframes",
    description="All MT5 timeframes with their enum values (21 supported: M1..MN1).",
    response_model=AvailableTimeframesResponse,
)
def get_chart_available_timeframes(ctx: BridgeContext, params: None) -> dict[str, Any]:
    def job(mt5: Any):
        return {name: int(getattr(mt5, f"TIMEFRAME_{name}")) for name in TIMEFRAME_NAMES if hasattr(mt5, f"TIMEFRAME_{name}")}

    values = ctx.driver.run(job, op="timeframes_enum")
    return {"timeframes": list(values.keys()), "enum_values": values}


@command(
    "get_chart_ohlcv",
    number=41,
    category="charts",
    title="Get OHLCV Series",
    description="Column-oriented OHLCV arrays for charting (time, open, high, low, close, tick_volume).",
    request_model=ChartHistoryRequest,
    response_model=OhlcvResponse,
)
def get_chart_ohlcv(ctx: BridgeContext, params: ChartHistoryRequest) -> dict[str, Any]:
    result = get_chart_history(ctx, params)
    bars = result["bars"]
    return {
        "symbol": result["symbol"],
        "timeframe": result["timeframe"],
        "count": len(bars),
        "time": [b["time"] for b in bars],
        "time_utc": [b["time_utc"] for b in bars],
        "open": [b["open"] for b in bars],
        "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars],
        "close": [b["close"] for b in bars],
        "tick_volume": [b["tick_volume"] or 0 for b in bars],
    }


@command(
    "get_chart_indicators",
    number=42,
    category="charts",
    title="Get Chart Indicators",
    description="Indicator availability. The MT5 terminal API does not expose GUI chart indicators — "
    "the bridge reports its own supported indicator set for server-side computation.",
    response_model=CapabilitiesNoteResponse,
)
def get_chart_indicators(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return {
        "available": False,
        "items": ["sma", "ema", "rsi", "atr", "macd", "bollinger"],
        "note": "GUI chart indicators are not exposed by the MT5 Python API. "
        "Series inputs for server-side indicator computation are available via get_chart_ohlcv.",
    }


@command(
    "get_chart_objects",
    number=43,
    category="charts",
    title="Get Chart Objects",
    description="Chart objects availability. The MT5 terminal API does not expose chart objects/drawings.",
    response_model=CapabilitiesNoteResponse,
)
def get_chart_objects(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return {
        "available": False,
        "items": [],
        "note": "Chart objects are not exposed by the MT5 Python API (MQL5-only capability).",
    }


@command(
    "get_chart_active_info",
    number=44,
    category="charts",
    title="Get Active Chart Info",
    description="The bridge's active chart context: configured symbol/timeframe, selection state, tick freshness.",
    response_model=ChartActiveInfoResponse,
)
def get_chart_active_info(ctx: BridgeContext, params: None) -> dict[str, Any]:
    symbol = ctx.settings.bot_symbol
    from .symbols import _fresh_tick

    tick = _fresh_tick(ctx, symbol)
    return {
        "symbol": symbol,
        "timeframe": ctx.settings.default_timeframe,
        "symbol_selected": True,
        "last_tick_utc": tick.time_utc,
        "note": "MT5 API cannot read the GUI chart selection; this reflects the bridge's configured chart context.",
    }


class MultiTimeframeRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    timeframes: list[str] = Field(..., min_length=1, max_length=21)
    count: int = Field(100, ge=1, le=10000)


@command(
    "get_chart_multi_timeframe_history",
    number=45,
    category="charts",
    title="Get Multi-Timeframe History",
    description="Candle history for several timeframes in one call (server-side MTF analysis support).",
    request_model=MultiTimeframeRequest,
    response_model=MultiTimeframeResponse,
)
def get_chart_multi_timeframe_history(ctx: BridgeContext, params: MultiTimeframeRequest) -> dict[str, Any]:
    series: dict[str, list[dict[str, Any]]] = {}
    dropped: dict[str, int] = {}
    for tf_name in params.timeframes:
        sub = get_chart_history(
            ctx,
            ChartHistoryRequest(symbol=params.symbol, timeframe=tf_name, count=params.count),
        )
        series[sub["timeframe"]] = sub["bars"]
        dropped[sub["timeframe"]] = sub["dropped"]
    return {
        "symbol": params.symbol.strip(),
        "count": params.count,
        "series": series,
        "dropped_by_timeframe": dropped,
    }

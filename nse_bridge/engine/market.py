"""Category 4 — real-time market data (28-36)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import MarketBookEntryModel
from .common import BridgeContext
from .registry import command
from .symbols import _fresh_tick, _symbol_param


class MarketTickResponse(BaseModel):
    tick: dict[str, Any]


class MarketSideResponse(BaseModel):
    symbol: str
    price: float
    time_utc: str | None = None


class MarketLastPriceResponse(BaseModel):
    symbol: str
    last_price: float
    source: str
    note: str | None = None


class MarketDepthResponse(BaseModel):
    symbol: str
    depth_levels: int
    entries: list[dict[str, Any]]
    note: str | None = None


class SpreadPoint(BaseModel):
    time_msc: int
    time_utc: str | None = None
    bid: float
    ask: float
    spread: float


class SpreadHistoryResponse(BaseModel):
    symbol: str
    minutes: int
    points: int
    avg_spread: float | None = None
    min_spread: float | None = None
    max_spread: float | None = None
    series: list[SpreadPoint]


class TickVolumeResponse(BaseModel):
    symbol: str
    tick_volume: float | None = None
    m1_bar_tick_volume: int | None = None
    time_utc: str | None = None


class WatchlistResponse(BaseModel):
    count: int
    symbols: list[str]


class SubscribeRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    interval_ms: int = Field(250, ge=50, le=5000)


class SubscribeResponse(BaseModel):
    subscription: dict[str, Any]
    current_tick: dict[str, Any]


@command(
    "get_market_tick",
    number=28,
    category="market",
    title="Get Latest Market Tick",
    description="Latest bid/ask tick for a symbol with freshness + staleness assessment.",
    response_model=MarketTickResponse,
)
def get_market_tick(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)
    return {"tick": tick.model_dump()}


@command(
    "get_market_bid",
    number=29,
    category="market",
    title="Get Market Bid",
    description="Current best bid price.",
    response_model=MarketSideResponse,
)
def get_market_bid(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)
    return {"symbol": symbol, "price": float(tick.bid or 0.0), "time_utc": tick.time_utc}


@command(
    "get_market_ask",
    number=30,
    category="market",
    title="Get Market Ask",
    description="Current best ask price.",
    response_model=MarketSideResponse,
)
def get_market_ask(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)
    return {"symbol": symbol, "price": float(tick.ask or 0.0), "time_utc": tick.time_utc}


@command(
    "get_market_last_price",
    number=31,
    category="market",
    title="Get Last Trade Price",
    description="Last executed price from the tick; falls back to the latest M1 bar close when the "
    "broker does not stream last-trade prices (typical for FX).",
    response_model=MarketLastPriceResponse,
)
def get_market_last_price(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)
    if tick.last and tick.last > 0:
        return {"symbol": symbol, "last_price": float(tick.last), "source": "tick"}
    # FX fallback: newest bar close (still a real broker price, honestly labeled)
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)

    bars = ctx.driver.run(job, op="copy_rates_from_pos", context={"symbol": symbol, "count": 1})
    if bars is None or len(bars) == 0:
        return {
            "symbol": symbol,
            "last_price": 0.0,
            "source": "unavailable",
            "note": "No last-deal price and no bar history available.",
        }
    bar = bars[-1]
    from ..timeutil import broker_epoch_to_utc, iso

    return {
        "symbol": symbol,
        "last_price": float(bar["close"]),
        "source": "m1_bar_close",
        "note": "Broker does not stream last-trade prices for this symbol (FX). Latest sealed M1 close used.",
        "time_utc": iso(broker_epoch_to_utc(int(bar["time"]), ctx.settings.broker_utc_offset_minutes)),
    }


@command(
    "get_market_depth",
    number=32,
    category="market",
    title="Get Market Depth",
    description="Order book (DOM) snapshot via market_book. Empty when the broker provides no depth for the symbol.",
    response_model=MarketDepthResponse,
)
def get_market_depth(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    ctx.driver.ensure_connected()
    from ..mt5.types import ORDER_TYPE_LABELS

    def job(mt5: Any):
        if not mt5.market_book_add(symbol):
            return None
        try:
            book = mt5.market_book_get(symbol)
            return list(book) if book else []
        finally:
            mt5.market_book_release(symbol)

    raw = ctx.driver.run(job, op="market_book", context={"symbol": symbol})
    if raw is None:
        return {
            "symbol": symbol,
            "depth_levels": 0,
            "entries": [],
            "note": "Broker provides no market depth for this symbol (market_book_add failed).",
        }
    entries = []
    for row in raw:
        row_type = int(getattr(row, "type", -1))
        entries.append(
            MarketBookEntryModel(
                type=row_type,
                type_label=ORDER_TYPE_LABELS.get(row_type, f"BOOK_{row_type}"),
                price=float(getattr(row, "price", 0.0)),
                volume=float(getattr(row, "volume", 0.0)),
                volume_dbl=float(getattr(row, "volume_dbl", 0.0)),
            ).model_dump()
        )
    return {"symbol": symbol, "depth_levels": len(entries), "entries": entries}


@command(
    "get_market_spread_history",
    number=33,
    category="market",
    title="Get Spread History",
    description="Bid/ask spread series over the last N minutes from tick history + summary stats.",
    request_model=None,
    response_model=SpreadHistoryResponse,
)
def get_market_spread_history(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    p = params or {}
    minutes = int(p.get("minutes", 60))
    limit = int(p.get("limit", 500))
    minutes = max(1, min(minutes, 1440))
    ctx.driver.ensure_connected()
    offset = ctx.settings.broker_utc_offset_minutes
    now = datetime.now(UTC) + timedelta(minutes=offset)  # broker timebase
    from_dt = now - timedelta(minutes=minutes)
    to_dt = now

    def job(mt5: Any):
        return mt5.copy_ticks_range(symbol, from_dt, to_dt, mt5.COPY_TICKS_ALL)

    raw = ctx.driver.run(job, op="copy_ticks_range", context={"symbol": symbol, "minutes": minutes})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            f"No tick history available for '{symbol}' in the last {minutes} minutes.",
            details={"symbol": symbol, "minutes": minutes},
        )
    from ..timeutil import broker_epoch_to_utc, iso

    series: list[SpreadPoint] = []
    spreads: list[float] = []
    from ..mt5.types import f as _f

    for row in raw:
        bid = float(_f(row, "bid", 0.0) or 0.0)
        ask = float(_f(row, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            continue
        spread = round(ask - bid, 8)
        msc = int(_f(row, "time_msc", 0) or 0)
        if not msc:
            continue
        utc = iso(broker_epoch_to_utc(msc / 1000.0, offset))
        series.append(SpreadPoint(time_msc=msc, time_utc=utc, bid=bid, ask=ask, spread=spread))
        spreads.append(spread)
    step = max(1, len(series) // limit)
    trimmed = series[::step][-limit:]
    return {
        "symbol": symbol,
        "minutes": minutes,
        "points": len(trimmed),
        "avg_spread": round(sum(spreads) / len(spreads), 8) if spreads else None,
        "min_spread": min(spreads) if spreads else None,
        "max_spread": max(spreads) if spreads else None,
        "series": [s.model_dump() for s in trimmed],
    }


@command(
    "get_market_tick_volume",
    number=34,
    category="market",
    title="Get Tick Volume",
    description="Tick volume of the latest tick and the newest M1 bar.",
    response_model=TickVolumeResponse,
)
def get_market_tick_volume(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)

    def job(mt5: Any):
        return mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)

    bars = ctx.driver.run(job, op="copy_rates_from_pos", context={"symbol": symbol, "count": 1})
    m1_tv = int(bars[-1]["tick_volume"]) if bars is not None and len(bars) else None
    return {
        "symbol": symbol,
        "tick_volume": tick.volume,
        "m1_bar_tick_volume": m1_tv,
        "time_utc": tick.time_utc,
    }


@command(
    "get_market_watchlist",
    number=35,
    category="market",
    title="Get Market Watch List",
    description="Symbols currently visible in the terminal's Market Watch panel.",
    response_model=WatchlistResponse,
)
def get_market_watchlist(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.symbols_get()

    raw = ctx.driver.run(job, op="symbols_get")
    names: list[str] = []
    if raw:
        for row in raw:
            if bool(getattr(row, "visible", False)):
                names.append(str(getattr(row, "name", "")))
    return {"count": len(names), "symbols": sorted(n for n in names if n)}


@command(
    "subscribe_market_ticks",
    number=36,
    category="market",
    title="Subscribe to Live Ticks",
    description="Register a live tick subscription for a symbol. Ticks are pushed to both WebSocket "
    "channels (dashboard + NSE server) at the requested interval.",
    request_model=SubscribeRequest,
    response_model=SubscribeResponse,
)
def subscribe_market_ticks(ctx: BridgeContext, params: SubscribeRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    sub = ctx.subscriptions.add(symbol, params.interval_ms)
    tick = _fresh_tick(ctx, symbol)
    payload = tick.model_dump()
    ctx.subscriptions.record_tick(symbol, payload)
    return {
        "subscription": {
            "symbol": sub.symbol,
            "interval_ms": sub.interval_ms,
            "created_at": sub.created_at,
        },
        "current_tick": payload,
    }

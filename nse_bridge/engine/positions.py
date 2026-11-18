"""Category 6 — positions management (46-54)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError
from ..mt5.types import build_position_model
from ..mt5.types import f as _f
from .common import BridgeContext
from .registry import command
from .trading import (
    close_position,
    fetch_position,
    fetch_positions,
    modify_position_sltp,
)


class PositionsRequest(BaseModel):
    symbol: str | None = Field(None, description="Filter by symbol")
    magic_filter: str = Field(
        "all",
        pattern="^(all|bot)$",
        description="all = every position (dashboard); bot = our magic only "
        "(matches nothing while no bridge magic is configured)",
    )


class PositionsResponse(BaseModel):
    count: int
    positions: list[dict[str, Any]]
    floating_profit: float


class TicketRequest(BaseModel):
    ticket: int = Field(..., gt=0)


class PositionInfoResponse(BaseModel):
    position: dict[str, Any]


class PositionProfitResponse(BaseModel):
    ticket: int
    symbol: str
    profit: float
    swap: float | None = None


class PositionVolumeResponse(BaseModel):
    ticket: int
    symbol: str
    volume: float


class PositionSlTpResponse(BaseModel):
    ticket: int
    symbol: str
    sl: float | None = None
    tp: float | None = None


class PositionRiskResponse(BaseModel):
    ticket: int
    symbol: str
    risk_amount: float | None = None
    risk_points: float | None = None
    currency: str | None = None
    note: str | None = None


class ModifySlTpRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    sl: float = Field(0.0, ge=0.0, description="0 = remove SL")
    tp: float = Field(0.0, ge=0.0, description="0 = remove TP")


class ClosePositionRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    volume: float | None = Field(None, gt=0.0, description="Partial close volume; None = full close")


class CloseAllRequest(BaseModel):
    symbol: str | None = None
    magic: int | None = Field(
        None,
        ge=0,
        description="Only close positions carrying this magic "
        "(RECOMMENDED: pass the bridge magic so the user's manual positions are untouched)",
    )


class CloseAllResponse(BaseModel):
    requested: int
    closed: int
    failed: int
    failures: list[dict[str, Any]]


@command(
    "get_trading_open_positions",
    number=46,
    category="positions",
    title="Get Open Positions",
    description="All open positions (optionally filtered by symbol; 'bot' filter keeps our magic only).",
    request_model=PositionsRequest,
    response_model=PositionsResponse,
)
def get_trading_open_positions(ctx: BridgeContext, params: PositionsRequest | None) -> dict[str, Any]:
    p = params or PositionsRequest()
    raw = fetch_positions(ctx, p.symbol.strip() if p.symbol else None)
    positions = []
    floating = 0.0
    for row in raw:
        if p.magic_filter == "bot":
            # "bot" = positions carrying the configured bridge magic.
            # With no magic configured there is no bridge identity, so
            # the filter matches nothing (never the manual magic-0
            # positions, and never every position).
            bridge_magic = ctx.settings.magic
            if bridge_magic is None or int(_f(row, "magic", -1)) != int(bridge_magic):
                continue
        model = build_position_model(row, ctx.settings.broker_utc_offset_minutes)
        floating += float(_f(row, "profit", 0.0) or 0.0)
        positions.append(model.model_dump())
    return {"count": len(positions), "positions": positions, "floating_profit": round(floating, 8)}


@command(
    "get_position_info",
    number=47,
    category="positions",
    title="Get Position Info",
    description="Full details of one open position by ticket.",
    request_model=TicketRequest,
    response_model=PositionInfoResponse,
)
def get_position_info(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    return {"position": build_position_model(raw, ctx.settings.broker_utc_offset_minutes).model_dump()}


@command(
    "get_position_profit",
    number=48,
    category="positions",
    title="Get Position Profit",
    description="Current floating profit/loss of one position.",
    request_model=TicketRequest,
    response_model=PositionProfitResponse,
)
def get_position_profit(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    return {
        "ticket": params.ticket,
        "symbol": str(_f(raw, "symbol", "")),
        "profit": float(_f(raw, "profit", 0.0) or 0.0),
        "swap": _f(raw, "swap"),
    }


@command(
    "get_position_volume",
    number=49,
    category="positions",
    title="Get Position Volume",
    description="Open volume (lots) of one position.",
    request_model=TicketRequest,
    response_model=PositionVolumeResponse,
)
def get_position_volume(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    return {
        "ticket": params.ticket,
        "symbol": str(_f(raw, "symbol", "")),
        "volume": float(_f(raw, "volume", 0.0) or 0.0),
    }


@command(
    "get_position_sl_tp",
    number=50,
    category="positions",
    title="Get Position SL/TP",
    description="Current stop-loss and take-profit of one position.",
    request_model=TicketRequest,
    response_model=PositionSlTpResponse,
)
def get_position_sl_tp(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    return {
        "ticket": params.ticket,
        "symbol": str(_f(raw, "symbol", "")),
        "sl": _f(raw, "sl"),
        "tp": _f(raw, "tp"),
    }


@command(
    "get_position_risk",
    number=51,
    category="positions",
    title="Get Position Risk",
    description="Risk exposure: loss-at-SL (broker-native order_calc_profit) + stop distance in points.",
    request_model=TicketRequest,
    response_model=PositionRiskResponse,
)
def get_position_risk(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    symbol = str(_f(raw, "symbol", ""))
    pos_type = int(_f(raw, "type", -1))
    volume = float(_f(raw, "volume", 0.0) or 0.0)
    entry = float(_f(raw, "price_open", 0.0) or 0.0)
    sl = float(_f(raw, "sl", 0.0) or 0.0)
    point = None
    ctx.driver.ensure_connected()

    def info_job(mt5: Any):
        return mt5.symbol_info(symbol)

    raw_info = ctx.driver.run(info_job, op="symbol_info", context={"symbol": symbol})
    point = float(_f(raw_info, "point", 0.0) or 0.0)

    risk_amount = None
    note = None
    if sl > 0 and entry > 0:

        def calc_job(mt5: Any):
            return mt5.order_calc_profit(pos_type, symbol, volume, entry, sl)

        val = ctx.driver.run(calc_job, op="order_calc_profit", context={"symbol": symbol, "ticket": params.ticket})
        if val is not None:
            risk_amount = abs(float(val))
        else:
            note = "order_calc_profit unavailable for this symbol."
    else:
        note = "No stop-loss set — open risk is unlimited; risk_amount not computable."
    risk_points = abs(entry - sl) / point if (sl > 0 and entry > 0 and point > 0) else None
    return {
        "ticket": params.ticket,
        "symbol": symbol,
        "risk_amount": round(risk_amount, 8) if risk_amount is not None else None,
        "risk_points": round(risk_points, 1) if risk_points is not None else None,
        "currency": str(_f(raw_info, "currency_profit", "") or "") or None,
        "note": note,
    }


@command(
    "trade_modify_sl_tp",
    number=52,
    category="positions",
    title="Modify Position SL/TP",
    description="Change stop-loss and/or take-profit of an open position (TRADE_ACTION_SLTP, retries, guarded).",
    request_model=ModifySlTpRequest,
    response_model=object,
    mutates=True,
)
def trade_modify_sl_tp(ctx: BridgeContext, params: ModifySlTpRequest) -> dict[str, Any]:
    return modify_position_sltp(ctx, params.ticket, params.sl, params.tp)


@command(
    "trade_close_single_position",
    number=53,
    category="positions",
    title="Close Single Position",
    description="Close one position by ticket (full or partial via volume). Ambiguous retcodes are "
    "re-checked against broker state — no duplicate closes.",
    request_model=ClosePositionRequest,
    response_model=object,
    mutates=True,
)
def trade_close_single_position(ctx: BridgeContext, params: ClosePositionRequest) -> dict[str, Any]:
    return close_position(ctx, params.ticket, params.volume)


@command(
    "trade_close_all_positions",
    number=54,
    category="positions",
    title="Close All Positions",
    description="Close every open position (optionally one symbol). Reports per-ticket failures.",
    request_model=CloseAllRequest,
    response_model=CloseAllResponse,
    mutates=True,
)
def trade_close_all_positions(ctx: BridgeContext, params: CloseAllRequest | None) -> dict[str, Any]:
    symbol = params.symbol.strip() if (params and params.symbol) else None
    raw = fetch_positions(ctx, symbol)
    magic_filter = params.magic if params else None
    targets = [row for row in raw if magic_filter is None or int(_f(row, "magic", -1)) == int(magic_filter)]
    closed, failed = 0, 0
    failures: list[dict[str, Any]] = []
    for row in targets:
        ticket = int(_f(row, "ticket", 0))
        try:
            close_position(ctx, ticket)
            closed += 1
        except BridgeError as exc:
            failed += 1
            failures.append({"ticket": ticket, "code": exc.code.value, "message": exc.message})
    return {"requested": len(targets), "closed": closed, "failed": failed, "failures": failures}

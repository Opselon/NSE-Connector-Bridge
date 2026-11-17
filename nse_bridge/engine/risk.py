"""Category 9 — risk management & calculations (73-81)."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import f as _f
from .common import BridgeContext, require_symbol_info
from .registry import command
from .symbols import _fresh_tick


def _value_per_price_unit(ctx: BridgeContext, symbol: str, spec: Any, tick: Any) -> float:
    """Account-currency value of a 1.0 price move for 1.0 lot.

    Source of truth: broker-native order_calc_profit over a $1 synthetic move
    (some brokers report misleading trade_tick_value fields). Falls back to
    tick_value/tick_size when the broker calc is unavailable.
    """
    entry = float(tick.ask or 0.0) if tick is not None else 0.0
    if entry > 0:
        ctx.driver.ensure_connected()

        def job(mt5: Any):
            return mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, entry, entry + 1.0)

        raw = ctx.driver.run(job, op="order_calc_profit", context={"symbol": symbol, "purpose": "value_per_unit"})
        if raw is not None and float(raw) > 0:
            return float(raw)
    tick_value = float(spec.trade_tick_value or 0.0)
    tick_size = float(spec.trade_tick_size or 0.0) or float(spec.point or 0.0)
    if tick_value <= 0 or tick_size <= 0:
        return 0.0
    return tick_value / tick_size


def _pip_size(spec: Any) -> float:
    """pip = 10*point (0.0001 on 5-digit FX, 0.1 on 2-digit gold)."""
    return float(spec.point or 0.0) * 10.0


def _round_price(price: float, spec: Any) -> float:
    digits = int(spec.digits or 2)
    tick_size = float(spec.trade_tick_size or 0.0) or float(spec.point or 0.00001)
    if tick_size > 0:
        return round(round(price / tick_size) * tick_size, digits)
    return round(price, digits)


class PositionSizeRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    risk_percent: float | None = Field(None, gt=0.0, le=100.0, description="Risk as % of balance")
    risk_amount: float | None = Field(None, gt=0.0, description="Risk in account currency")
    stop_points: float | None = Field(None, gt=0.0, description="Stop distance in broker points")
    stop_price: float | None = Field(None, gt=0.0, description="Absolute stop price (alternative)")
    entry_price: float | None = Field(None, gt=0.0, description="Default: current market")


class PositionSizeResponse(BaseModel):
    symbol: str
    lots: float
    raw_lots: float
    risk_amount: float
    stop_distance_price: float
    stop_distance_points: float
    value_per_price_unit_per_lot: float
    volume_min: float
    volume_max: float
    volume_step: float
    clamped_to_min: bool
    note: str | None = None


@command(
    "calculate_position_size",
    number=73,
    category="risk",
    title="Calculate Position Size",
    description="Optimal lot size for a risk budget (% of balance or fixed amount) and a stop distance. "
    "Rounded DOWN to the broker volume step.",
    request_model=PositionSizeRequest,
    response_model=PositionSizeResponse,
)
def calculate_position_size(ctx: BridgeContext, params: PositionSizeRequest) -> dict[str, Any]:
    if params.risk_percent is None and params.risk_amount is None:
        raise BridgeError(
            ErrorCode.VALIDATION, "Provide risk_percent or risk_amount.", http_status=422
        )
    if params.stop_points is None and params.stop_price is None:
        raise BridgeError(
            ErrorCode.VALIDATION, "Provide stop_points or stop_price.", http_status=422
        )
    symbol = params.symbol.strip()
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    entry = params.entry_price or (float(tick.ask) if params.side == "buy" else float(tick.bid))

    if params.stop_price:
        stop_distance = abs(entry - float(params.stop_price))
        if stop_distance <= 0:
            raise BridgeError(ErrorCode.VALIDATION, "stop_price equals entry price.", http_status=422)
    else:
        stop_distance = float(params.stop_points) * float(spec.point or 0.0)

    from .common import account_info_model

    acc = account_info_model(ctx)
    risk_money = (
        float(params.risk_amount)
        if params.risk_amount is not None
        else float(acc.balance or 0.0) * float(params.risk_percent) / 100.0
    )
    vpu = _value_per_price_unit(ctx, symbol, spec, tick)
    if vpu <= 0:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "tick_value/tick_size unavailable for sizing on this symbol.",
            details={"symbol": symbol},
        )
    risk_per_lot = stop_distance * vpu
    if risk_per_lot <= 0:
        raise BridgeError(ErrorCode.VALIDATION, "Stop distance must be positive.", http_status=422)
    raw_lots = risk_money / risk_per_lot
    vol_min = float(spec.volume_min or 0.01)
    vol_max = float(spec.volume_max or 100.0)
    vol_step = float(spec.volume_step or 0.01)
    lots = math.floor(raw_lots / vol_step) * vol_step if vol_step > 0 else raw_lots
    clamped = False
    note = None
    if lots < vol_min:
        lots = vol_min
        clamped = True
        note = f"Raw size {raw_lots:.4f} below volume_min — clamped to {vol_min} (risk exceeds budget)."
    if lots > vol_max:
        lots = vol_max
        clamped = True
        note = "Raw size above volume_max — clamped."
    lots = round(lots, 8)
    return {
        "symbol": symbol,
        "lots": lots,
        "raw_lots": round(raw_lots, 6),
        "risk_amount": round(risk_money, 2),
        "stop_distance_price": round(stop_distance, 8),
        "stop_distance_points": round(stop_distance / float(spec.point or 1e-9), 1),
        "value_per_price_unit_per_lot": round(vpu, 8),
        "volume_min": vol_min,
        "volume_max": vol_max,
        "volume_step": vol_step,
        "clamped_to_min": clamped,
        "note": note,
        "balance": acc.balance,
        "currency": acc.currency,
    }


class TradeRiskRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    volume: float = Field(..., gt=0.0)
    entry_price: float | None = Field(None, gt=0.0)
    stop_price: float | None = Field(None, gt=0.0, description="Absolute SL price")
    stop_points: float | None = Field(None, gt=0.0, description="SL distance in points")


class TradeRiskResponse(BaseModel):
    symbol: str
    risk_amount: float | None = None
    risk_points: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    currency: str | None = None
    source: str
    note: str | None = None


@command(
    "calculate_trade_risk",
    number=74,
    category="risk",
    title="Calculate Trade Risk",
    description="Money at risk for a volume and stop (broker-native order_calc_profit with formula fallback).",
    request_model=TradeRiskRequest,
    response_model=TradeRiskResponse,
)
def calculate_trade_risk(ctx: BridgeContext, params: TradeRiskRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    entry = params.entry_price or (float(tick.ask) if params.side == "buy" else float(tick.bid))
    point = float(spec.point or 0.0)
    if params.stop_price:
        stop = float(params.stop_price)
    elif params.stop_points:
        stop = entry - float(params.stop_points) * point if params.side == "buy" else entry + float(params.stop_points) * point
    else:
        raise BridgeError(ErrorCode.VALIDATION, "Provide stop_price or stop_points.", http_status=422)
    risk_points = abs(entry - stop) / point if point > 0 else None
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        op_type = mt5.ORDER_TYPE_BUY if params.side == "buy" else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_profit(op_type, symbol, float(params.volume), entry, stop)

    raw = ctx.driver.run(job, op="order_calc_profit", context={"symbol": symbol, "volume": params.volume})
    if raw is not None:
        return {
            "symbol": symbol,
            "risk_amount": round(abs(float(raw)), 8),
            "risk_points": round(risk_points, 1) if risk_points else None,
            "entry_price": round(entry, 8),
            "stop_price": round(stop, 8),
            "currency": spec.currency_profit or "USD",
            "source": "BROKER_NATIVE",
        }
    vpu = _value_per_price_unit(ctx, symbol, spec, tick)
    risk = abs(entry - stop) * vpu * float(params.volume)
    return {
        "symbol": symbol,
        "risk_amount": round(risk, 8) if risk else None,
        "risk_points": round(risk_points, 1) if risk_points else None,
        "entry_price": round(entry, 8),
        "stop_price": round(stop, 8),
        "currency": spec.currency_profit or "USD",
        "source": "FORMULA_FALLBACK",
        "note": "order_calc_profit unavailable — computed from tick_value/tick_size.",
    }


class StopPriceRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    stop_points: float = Field(..., gt=0.0)
    entry_price: float | None = Field(None, gt=0.0)


class PriceLevelResponse(BaseModel):
    symbol: str
    side: str
    entry_price: float
    price: float
    distance_points: float
    point: float
    tick_aligned: bool


def _level(ctx: BridgeContext, params, *, kind: str) -> dict[str, Any]:
    symbol = params.symbol.strip()
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    point = float(spec.point or 0.0)
    entry = params.entry_price or (float(tick.ask) if params.side == "buy" else float(tick.bid))
    distance = float(params.stop_points) * point
    if kind == "sl":
        price = entry - distance if params.side == "buy" else entry + distance
    else:
        price = entry + distance if params.side == "buy" else entry - distance
    price = _round_price(price, spec)
    tick_size = float(spec.trade_tick_size or 0.0) or point
    aligned = abs(round(price / tick_size) * tick_size - price) < 1e-9 if tick_size > 0 else True
    return {
        "symbol": symbol,
        "side": params.side,
        "entry_price": round(entry, 8),
        "price": price,
        "distance_points": float(params.stop_points),
        "point": point,
        "tick_aligned": aligned,
    }


@command(
    "calculate_stop_loss_price",
    number=75,
    category="risk",
    title="Calculate Stop Loss Price",
    description="SL price for a side and stop distance in points, tick-size aligned.",
    request_model=StopPriceRequest,
    response_model=PriceLevelResponse,
)
def calculate_stop_loss_price(ctx: BridgeContext, params: StopPriceRequest) -> dict[str, Any]:
    return _level(ctx, params, kind="sl")


@command(
    "calculate_take_profit_price",
    number=76,
    category="risk",
    title="Calculate Take Profit Price",
    description="TP price for a side and reward distance in points, tick-size aligned.",
    request_model=StopPriceRequest,
    response_model=PriceLevelResponse,
)
def calculate_take_profit_price(ctx: BridgeContext, params: StopPriceRequest) -> dict[str, Any]:
    return _level(ctx, params, kind="tp")


class MarginRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    volume: float = Field(..., gt=0.0)
    price: float | None = Field(None, gt=0.0)


class MarginResponse(BaseModel):
    symbol: str
    margin_required: float | None = None
    currency: str | None = None
    source: str
    note: str | None = None


@command(
    "calculate_margin_required",
    number=77,
    category="risk",
    title="Calculate Margin Required",
    description="Broker-native margin for a volume/price (order_calc_margin).",
    request_model=MarginRequest,
    response_model=MarginResponse,
)
def calculate_margin_required(ctx: BridgeContext, params: MarginRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    price = params.price or (float(tick.ask) if params.side == "buy" else float(tick.bid))
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        op_type = mt5.ORDER_TYPE_BUY if params.side == "buy" else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_margin(op_type, symbol, float(params.volume), price)

    raw = ctx.driver.run(job, op="order_calc_margin", context={"symbol": symbol, "volume": params.volume})
    if raw is None or float(raw) <= 0:
        # formula fallback: margin_initial based (approximation)
        initial = float(spec.margin_initial or 0.0)
        contract = float(spec.trade_contract_size or 0.0)
        if initial > 0 and contract > 0:
            approx = initial * float(params.volume) * price / contract
            return {
                "symbol": symbol,
                "margin_required": round(approx, 8),
                "currency": spec.currency_margin or spec.currency_profit or "USD",
                "source": "FORMULA_FALLBACK",
                "note": "order_calc_margin unavailable — margin_initial approximation used.",
            }
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "order_calc_margin unavailable for this symbol.",
            details={"symbol": symbol},
        )
    return {
        "symbol": symbol,
        "margin_required": round(float(raw), 8),
        "currency": spec.currency_margin or spec.currency_profit or "USD",
        "source": "BROKER_NATIVE",
    }


class MarginLevelRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    volume: float = Field(..., gt=0.0)
    price: float | None = Field(None, gt=0.0)


class MarginLevelAfterResponse(BaseModel):
    equity: float
    current_margin: float
    projected_margin: float
    projected_margin_level: float | None
    currency: str | None = None
    note: str | None = None


@command(
    "calculate_margin_level_after_trade",
    number=78,
    category="risk",
    title="Calculate Margin Level After Trade",
    description="Predicted margin level (equity / projected margin * 100) if the trade is opened.",
    request_model=MarginLevelRequest,
    response_model=MarginLevelAfterResponse,
)
def calculate_margin_level_after_trade(ctx: BridgeContext, params: MarginLevelRequest) -> dict[str, Any]:
    from .common import account_info_model

    acc = account_info_model(ctx)
    margin_req = calculate_margin_required(
        ctx,
        MarginRequest(symbol=params.symbol, side=params.side, volume=params.volume, price=params.price),
    )
    projected = float(acc.margin or 0.0) + float(margin_req["margin_required"] or 0.0)
    level = float(acc.equity or 0.0) / projected * 100.0 if projected > 0 else None
    note = None
    if level is not None and level < 100:
        note = "WARNING: projected margin level below 100% — margin call risk."
    elif level is not None and level < 200:
        note = "Projected margin level is tight (<200%)."
    return {
        "equity": float(acc.equity or 0.0),
        "current_margin": float(acc.margin or 0.0),
        "projected_margin": round(projected, 8),
        "projected_margin_level": round(level, 2) if level is not None else None,
        "currency": acc.currency,
        "note": note,
        "margin_source": margin_req["source"],
    }


class PipValueRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    volume: float = Field(1.0, gt=0.0)


class PipValueResponse(BaseModel):
    symbol: str
    pip_size: float
    pip_value: float
    volume: float
    currency: str | None = None


@command(
    "calculate_pip_value",
    number=79,
    category="risk",
    title="Calculate Pip Value",
    description="Account-currency value of one pip for a volume (pip = 10*point). Uses broker-native "
    "order_calc_profit as the source of truth, tick-value formula as fallback.",
    request_model=PipValueRequest,
    response_model=PipValueResponse,
)
def calculate_pip_value(ctx: BridgeContext, params: PipValueRequest) -> dict[str, Any]:
    symbol = params.symbol.strip()
    spec = require_symbol_info(ctx, symbol)
    pip = _pip_size(spec)
    tick = _fresh_tick(ctx, symbol)
    entry = float(tick.ask or 0.0)
    source = "FORMULA_FALLBACK"
    note = None
    pip_value = None
    if entry > 0:
        ctx.driver.ensure_connected()

        def job(mt5: Any):
            return mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, float(params.volume), entry, entry + pip)

        raw = ctx.driver.run(job, op="order_calc_profit", context={"symbol": symbol, "purpose": "pip_value"})
        if raw is not None:
            pip_value = abs(float(raw))
            source = "BROKER_NATIVE"
    if pip_value is None:
        vpu = _value_per_price_unit(ctx, symbol, spec, tick)
        if vpu <= 0:
            raise BridgeError(
                ErrorCode.MT5_CALL_FAILED,
                "tick_value unavailable for pip value on this symbol.",
                details={"symbol": symbol},
            )
        pip_value = pip * vpu * float(params.volume)
        note = "Computed from tick_value/tick_size (broker calc unavailable)."
    return {
        "symbol": symbol,
        "pip_size": pip,
        "pip_value": round(pip_value, 8),
        "volume": params.volume,
        "currency": spec.currency_profit or "USD",
        "source": source,
        "note": note,
    }


class ProfitLossRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    volume: float = Field(..., gt=0.0)
    entry_price: float = Field(..., gt=0.0)
    exit_price: float = Field(..., gt=0.0)


@command(
    "calculate_profit_loss",
    number=80,
    category="risk",
    title="Calculate Profit/Loss",
    description="Broker-native PnL for a hypothetical entry/exit (order_calc_profit).",
    request_model=ProfitLossRequest,
    response_model=object,
)
def calculate_profit_loss(ctx: BridgeContext, params: ProfitLossRequest) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        op_type = mt5.ORDER_TYPE_BUY if params.side == "buy" else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_profit(
            op_type, params.symbol, float(params.volume), float(params.entry_price), float(params.exit_price)
        )

    raw = ctx.driver.run(job, op="order_calc_profit", context={"symbol": params.symbol})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "order_calc_profit unavailable for this symbol.",
            details={"symbol": params.symbol},
        )
    spec = require_symbol_info(ctx, params.symbol)
    return {
        "symbol": params.symbol,
        "side": params.side,
        "volume": params.volume,
        "entry_price": params.entry_price,
        "exit_price": params.exit_price,
        "profit": float(raw),
        "currency": spec.currency_profit or "USD",
        "source": "BROKER_NATIVE",
    }


class ValidateTradeRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field("buy", pattern="^(buy|sell)$")
    volume: float = Field(..., gt=0.0)
    sl: float = Field(0.0, ge=0.0)
    tp: float = Field(0.0, ge=0.0)
    price: float | None = Field(None, gt=0.0, description="Pending order price (None = market)")


class ValidateTradeResponse(BaseModel):
    valid: bool
    structural: dict[str, Any]
    broker: dict[str, Any]
    permissions: dict[str, Any]


@command(
    "validate_trade_request",
    number=81,
    category="risk",
    title="Validate Trade Request",
    description="Full pre-trade validation WITHOUT dispatching: structural geometry (volume/SL/TP/stops "
    "distances vs live tick) + broker-side order_check + permission state.",
    request_model=ValidateTradeRequest,
    response_model=ValidateTradeResponse,
)
def validate_trade_request(ctx: BridgeContext, params: ValidateTradeRequest) -> dict[str, Any]:
    from .execution import validate_pending

    symbol = params.symbol.strip()
    # structural geometry (shared with the pending-order validator; price None = market check)
    price_for_check = params.price or 0.0
    structural_reasons: list[str] = []
    if params.price is not None:
        structural_reasons = validate_pending(
            ctx, symbol, "BUY_LIMIT" if params.side == "buy" else "SELL_LIMIT",
            params.price, params.sl, params.tp, params.volume,
        )
    else:
        spec = require_symbol_info(ctx, symbol)
        vol_min = float(spec.volume_min or 0.0)
        vol_max = float(spec.volume_max or 0.0)
        vol_step = float(spec.volume_step or 0.01)
        if params.volume < vol_min - 1e-9:
            structural_reasons.append("volume_below_min")
        if params.volume > vol_max + 1e-9:
            structural_reasons.append("volume_above_max")
        if vol_step > 0:
            steps = round(params.volume / vol_step)
            if abs(params.volume - steps * vol_step) > 1e-9:
                structural_reasons.append("volume_not_multiple_of_step")

    # broker-side order_check (no dispatch)
    tick = _fresh_tick(ctx, symbol)
    price = params.price or (float(tick.ask) if params.side == "buy" else float(tick.bid))

    def job(mt5: Any):
        op_type = mt5.ORDER_TYPE_BUY if params.side == "buy" else mt5.ORDER_TYPE_SELL
        return mt5.order_check({
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(params.volume),
            "type": op_type,
            "price": float(price),
            "sl": float(params.sl),
            "tp": float(params.tp),
        })

    check = ctx.driver.run(job, op="order_check", context={"symbol": symbol})
    broker = {}
    if check is None:
        broker = {"available": False, "note": "order_check returned None"}
    else:
        broker = {
            "available": True,
            "retcode": int(_f(check, "retcode", 0)),
            "comment": _f(check, "comment"),
            "margin_required": _f(check, "margin"),
            "margin_free_after": _f(check, "margin_free"),
            "filling_mode": _f(check, "filling_mode"),
        }

    # permissions
    from .common import account_info_model

    acc = account_info_model(ctx)
    permissions = {
        "account_trade_allowed": acc.trade_allowed,
        "account_trade_expert": acc.trade_expert,
        "trade_mode": acc.trade_mode,
    }

    valid = not structural_reasons and (not broker or broker.get("retcode", 0) == 0)
    return {
        "valid": bool(valid),
        "structural": {"ok": not structural_reasons, "reasons": structural_reasons},
        "broker": broker,
        "permissions": permissions,
        "request_echo": params.model_dump(),
    }

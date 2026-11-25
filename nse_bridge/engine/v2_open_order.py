"""Client-side v2 ``open_order`` hooks (S4, v1.3.0) — BEHIND A FLAG.

IMPORTANT — the Bridge alone does NOT enable new-position trading.
Opening a new position requires the Platform P3 protocol: the
Platform must ship the P3 contract (risk engine, financial consent,
kill-switch enforcement), negotiate protocol v2 with this Bridge
(a ``schema_version`` bump the Platform publishes), and the operator
must explicitly enable the local flag. Until ALL of that exists,
every call here refuses loudly and nothing reaches MT5.

This module is deliberately NOT wired into the live command path:
:mod:`nse_bridge.server.commands` never imports it, the §17.5
activation gate (``open_order_authorized=False``, no config knob)
still rejects server-sent ``open_order`` frames as
``UnsupportedOperation``, and the WS channel never constructs a v2
negotiation. The P3 work will call :func:`execute_v2_open_order`
only after negotiation is implemented; until then this module is a
tested, dormant hook — proof that the client side is ready, not a
live trading surface.

Two independent gates, both default CLOSED:

1. ``flag_enabled`` — the operator opt-in
   (``BridgeSettings.enable_v2_open_order``,
   env ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=1``; default False).
2. ``negotiation`` — a :class:`V2Negotiation` the Platform must
   establish during the v2 handshake (``negotiated=True`` plus the
   negotiated ``schema_version``). There is no v2 handshake in
   CONTRACT v1, so no production code can construct a negotiated
   instance today; the P3 work owns that wire shape.

Refusal is loud and typed: :class:`BridgeError` with
``ErrorCode.DISABLED`` (``FEATURE_DISABLED``) and a message naming
exactly which gate is closed and what is missing. A refused call
never touches the driver (zero ``order_send`` calls).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from loguru import logger

from ..errors import BridgeError, ErrorCode
from .common import BridgeContext
from .execution import PendingOrderRequest, trade_send_pending_order
from .trading import market_order

#: The negotiated v2 contract version is recorded here once the
#: Platform publishes it (P3). Until then there is no v2 schema to
#: negotiate, and ``V2Negotiation.negotiated`` can never be True in
#: production.
V2_SCHEMA_VERSION: str | None = None


@dataclass(frozen=True)
class V2Negotiation:
    """Result of the Platform v2 handshake (P3-owned wire shape).

    ``negotiated`` is True only after the Platform has completed a
    v2 handshake with this Bridge (contract version bump, risk-engine
    and consent proof). ``schema_version`` records the negotiated
    contract version for the audit trail. No production code path
    constructs a negotiated instance today — CONTRACT v1 defines no
    v2 handshake — so the default is unnegotiated.
    """

    negotiated: bool = False
    schema_version: str | None = None


@dataclass(frozen=True)
class V2OpenOrderRequest:
    """A v2 open request (market or pending entry)."""

    symbol: str
    side: Literal["buy", "sell"]
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    order_kind: Literal["market", "limit", "stop", "stop_limit"] = "market"
    price: float | None = None
    stoplimit: float | None = None
    comment: str = "NSE_OPEN"

    @classmethod
    def from_operation(cls, operation: Mapping[str, Any]) -> V2OpenOrderRequest:
        """Build a request from a Platform v2 ``operation`` object (§14.2).

        Accepts the Platform wire names ``order_type`` / ``stop_limit``
        as aliases of the Bridge-native ``order_kind`` / ``stoplimit``
        (Bridge name wins when both are present). A pending kind sent
        under either name is preserved — it must never silently fall
        back to ``"market"``. Unknown kinds and missing required
        fields fail loudly here, never at dispatch.
        """
        if not isinstance(operation, Mapping):
            raise TypeError("v2 open_order operation is not a JSON object")
        op_type = operation.get("type")
        if op_type is not None and str(op_type) != "open_order":
            raise ValueError(f"v2 open_order operation has unexpected type {op_type!r}")
        kind_raw = operation.get("order_kind")
        if kind_raw in (None, ""):
            kind_raw = operation.get("order_type")
        order_kind = str(kind_raw).strip().lower() if kind_raw not in (None, "") else "market"
        if order_kind not in ("market", "limit", "stop", "stop_limit"):
            raise ValueError(f"v2 open_order has unknown order kind {order_kind!r}")
        stoplimit_raw = operation.get("stoplimit")
        if stoplimit_raw is None:
            stoplimit_raw = operation.get("stop_limit")

        def _num(value: Any, name: str) -> float | None:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValueError(f"v2 open_order {name} is not a number") from None

        symbol = operation.get("symbol")
        if symbol in (None, ""):
            raise ValueError("v2 open_order operation is missing 'symbol'")
        side_raw = operation.get("side")
        if side_raw in (None, ""):
            raise ValueError("v2 open_order operation is missing 'side'")
        side = str(side_raw).strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"v2 open_order has unknown side {side!r}")
        volume = _num(operation.get("volume"), "volume")
        if volume is None:
            raise ValueError("v2 open_order operation is missing 'volume'")
        sl_value = _num(operation.get("sl"), "sl")
        tp_value = _num(operation.get("tp"), "tp")
        comment = operation.get("comment")
        return cls(
            symbol=str(symbol).strip(),
            side=cast(Literal["buy", "sell"], side),
            volume=volume,
            sl=sl_value if sl_value is not None else 0.0,
            tp=tp_value if tp_value is not None else 0.0,
            order_kind=cast(Literal["market", "limit", "stop", "stop_limit"], order_kind),
            price=_num(operation.get("price"), "price"),
            stoplimit=_num(stoplimit_raw, "stoplimit"),
            comment=str(comment) if comment not in (None, "") else "NSE_OPEN",
        )


def check_v2_open_order_available(
    *,
    flag_enabled: bool,
    negotiation: V2Negotiation | None,
) -> None:
    """Refuse loudly unless BOTH v2 gates are open.

    Raises ``BridgeError(ErrorCode.DISABLED)`` naming the closed gate;
    returns ``None`` when the hook may proceed. This is the single
    gate check — :func:`execute_v2_open_order` and any future P3
    caller must go through it, never re-implement it.
    """
    if not flag_enabled:
        logger.warning("[V2_OPEN_ORDER] refused — v2 flag is off (NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=1 required)")
        raise BridgeError(
            ErrorCode.DISABLED,
            "v2 open_order is DISABLED: the NSE_BRIDGE_ENABLE_V2_OPEN_ORDER flag is off. "
            "New-position trading requires the Platform P3 protocol — the Bridge alone does not enable it.",
            details={"gate": "flag", "flag": "enable_v2_open_order"},
        )
    if negotiation is None or not negotiation.negotiated:
        logger.warning("[V2_OPEN_ORDER] refused — protocol v2 has not been negotiated with the Platform")
        raise BridgeError(
            ErrorCode.DISABLED,
            "v2 open_order is DISABLED: protocol v2 has not been negotiated with the Platform. "
            "The Platform must ship the P3 contract and complete the v2 handshake (schema_version bump) first.",
            details={"gate": "negotiation", "negotiated": False},
        )
    if (
        negotiation.schema_version is not None
        and V2_SCHEMA_VERSION is not None
        and negotiation.schema_version != V2_SCHEMA_VERSION
    ):
        raise BridgeError(
            ErrorCode.DISABLED,
            f"v2 open_order refused: negotiated schema_version {negotiation.schema_version!r} "
            f"does not match the supported v2 schema {V2_SCHEMA_VERSION!r}.",
            details={"gate": "negotiation", "schema_version": negotiation.schema_version},
        )


def execute_v2_open_order(
    ctx: BridgeContext,
    request: V2OpenOrderRequest,
    *,
    flag_enabled: bool,
    negotiation: V2Negotiation | None,
) -> dict[str, Any]:
    """Execute a v2 open — ONLY after both gates pass.

    Dispatches through the proven local primitives
    (:func:`market_order` / :func:`trade_send_pending_order`), which
    carry the mandatory pre-send ``order_check`` and W4
    reconciliation. This function is NOT called by the server command
    path; the P3 work will call it after v2 negotiation.
    """
    check_v2_open_order_available(flag_enabled=flag_enabled, negotiation=negotiation)
    if request.volume <= 0:
        raise BridgeError(
            ErrorCode.VALIDATION,
            "v2 open_order requires a positive volume.",
            http_status=422,
            details={"volume": request.volume},
        )
    logger.info(
        "[V2_OPEN_ORDER] dispatching {} {} {} (schema_version={})",
        request.side,
        request.volume,
        request.symbol,
        negotiation.schema_version if negotiation else None,
    )
    if request.order_kind == "market":
        return market_order(
            ctx,
            symbol=request.symbol,
            side=request.side,
            volume=request.volume,
            sl=request.sl,
            tp=request.tp,
            comment=request.comment,
        )
    pending_types = {"limit": "LIMIT", "stop": "STOP", "stop_limit": "STOP_LIMIT"}
    if request.price is None or request.price <= 0:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"v2 open_order order_kind={request.order_kind} requires a positive price.",
            http_status=422,
            details={"order_kind": request.order_kind},
        )
    return trade_send_pending_order(
        ctx,
        PendingOrderRequest(
            symbol=request.symbol,
            order_type=f"{request.side.upper()}_{pending_types[request.order_kind]}",
            volume=request.volume,
            price=request.price,
            stoplimit=request.stoplimit,
            sl=request.sl,
            tp=request.tp,
            comment=request.comment,
        ),
    )

"""Client-side v2 ``open_order`` execution (P3 v2 protocol, Bridge v1.4.0 B3).

Wired into the live server command path by
:mod:`nse_bridge.server.commands` — a server-sent ``open_order``
frame reaches :func:`execute_v2_open_order` ONLY when every gate
below is open. The kill switch stays ENGAGED: this slice is code
only, no execution enablement beyond what the Platform authorizes
per command (the Platform's §6.4 gate refuses v2 commands while
its switch is engaged; the Bridge enforces its own gates here).

Two independent gates; the flag defaults OPEN, negotiation stays
fail-closed:

1. ``flag_enabled`` — the operator flag
   (``BridgeSettings.enable_v2_open_order``,
   env ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER``; default True, opt-out
   with ``=0`` / ``=false``).
2. ``negotiation`` — a :class:`V2Negotiation` built from the
   command envelope's ``protocol_version`` by
   :func:`negotiate_protocol_version`: ``"2"`` is honoured only
   when this Bridge speaks v2 (version >= ``1.3.0``, the Platform's
   ``V2_MIN_CONNECTOR_VERSION``). Any other value — absent, ``"1"``,
   unknown — negotiates down to v1, fail-closed, and a v2-only op
   is refused before it ever reaches MT5.

Fail-closed parsing (the PR #20 wire-bug lesson): the Platform
emits the v2 names ``order_type`` / ``stop_limit``; the Bridge's
native names are ``order_kind`` / ``stoplimit``. Both are accepted
(Bridge name wins when both are present). A pending kind sent under
either name is preserved — it must never silently fall back to
``"market"``. A kind that IS present but unknown fails loudly
here, never at dispatch. A kind that is ABSENT fails loudly too:
``V2OpenOrderRequest.order_kind`` is a required keyword-only
``Literal`` (no default — a direct construction without kind is a
``TypeError`` at runtime and a mypy error at type-check time), and
:func:`from_operation` raises ``ValueError`` on a wire operation
without ``order_kind``/``order_type``. The old CONTRACTS §7 market
default is gone (audit P2-3): a dropped kind field must never turn
a pending order into a market order, and a market open without an
explicit kind is indistinguishable from one — so both are rejected.
The Platform always sends ``order_type`` explicitly (see
``agent_trade::exe``).

Pre-send validation: :func:`execute_v2_open_order` validates the
request against live MT5 state (symbol resolvable, volume within
the broker min/max/step, SL/TP geometry against the live tick)
BEFORE dispatch, on top of the mandatory official ``order_check``
the primitives run (W2). A structurally invalid open is a
``VALIDATION`` failure and nothing is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from loguru import logger

from .. import BRIDGE_VERSION
from ..errors import BridgeError, ErrorCode
from .common import BridgeContext
from .execution import PendingOrderRequest, trade_send_pending_order, validate_market_open_params
from .trading import market_order

#: The negotiated v2 contract version (Platform
#: ``connector::contract::V2_SCHEMA_VERSION``). A handshake that
#: negotiates any other schema is refused.
V2_SCHEMA_VERSION: str = "2"

#: Minimum Bridge version that speaks protocol v2 (Platform
#: ``connector::contract::V2_MIN_CONNECTOR_VERSION``).
V2_MIN_CONNECTOR_VERSION: str = "1.3.0"

#: Max pending-order expiry, seconds from issue (Platform
#: ``connector::contract::COMMAND_TTL_MAX_SECS``). A larger
#: ``expiry_secs`` is refused, never clamped silently.
COMMAND_TTL_MAX_SECS: int = 900

#: Allowed deviation range for a market open, in MT5 points
#: (``MarketOrderRequest.deviation`` is 0..1000).
DEVIATION_MAX: int = 1000

ORDER_KINDS: tuple[str, str, str, str] = ("market", "limit", "stop", "stop_limit")


def version_at_least(version: str, minimum: str) -> bool:
    """Semver-ish ``version >= minimum`` over dotted numeric parts.

    Non-numeric components compare as 0 — a malformed version never
    qualifies as new enough (fail-closed).
    """

    def _parts(value: str) -> tuple[int, int, int]:
        out: list[int] = []
        for piece in value.strip().split(".")[:3]:
            try:
                out.append(int(piece))
            except ValueError:
                out.append(0)
        while len(out) < 3:
            out.append(0)
        return (out[0], out[1], out[2])

    return _parts(version) >= _parts(minimum)


def connector_supports_v2(bridge_version: str = BRIDGE_VERSION) -> bool:
    """Whether this Bridge speaks protocol v2 (§14.1 negotiation)."""
    return version_at_least(bridge_version, V2_MIN_CONNECTOR_VERSION)


@dataclass(frozen=True)
class V2Negotiation:
    """Result of the Platform v2 handshake (P3-owned wire shape).

    ``negotiated`` is True only after the Platform stamped
    ``protocol_version: "2"`` on the command envelope AND this
    Bridge speaks v2 (see :func:`negotiate_protocol_version`).
    ``schema_version`` records the negotiated contract version for
    the audit trail.
    """

    negotiated: bool = False
    schema_version: str | None = None


def negotiate_protocol_version(
    requested: str | None,
    bridge_version: str = BRIDGE_VERSION,
) -> V2Negotiation:
    """Negotiate the protocol version for one command (§14.1).

    A ``"2"`` request is honoured ONLY when this Bridge speaks v2;
    anything else — absent, ``"1"``, unknown — negotiates down to
    v1, fail-closed. The caller records the negotiated version and
    refuses v2-only ops on a v1 negotiation.
    """
    if requested == "2" and connector_supports_v2(bridge_version):
        return V2Negotiation(negotiated=True, schema_version=V2_SCHEMA_VERSION)
    if requested not in (None, "1", "2"):
        logger.warning("[V2] unknown protocol_version {!r} — negotiating down to v1 (fail-closed)", requested)
    return V2Negotiation(negotiated=False, schema_version=None)


@dataclass(frozen=True)
class V2OpenOrderRequest:
    """A v2 open request (market or pending entry).

    ``order_kind`` is required and keyword-only (audit P2-3): it
    has no default, so a construction without kind fails at
    type-check time (mypy: missing argument) and at runtime
    (``TypeError``) instead of silently becoming a market order.
    It must come after the defaulted ``sl``/``tp`` fields, so
    keyword-only is the only shape that can be required.
    """

    symbol: str
    side: Literal["buy", "sell"]
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    order_kind: Literal["market", "limit", "stop", "stop_limit"] = field(kw_only=True)
    price: float | None = None
    stoplimit: float | None = None
    #: Allowed deviation in MT5 points (0 = no deviation tolerated).
    deviation: int = 20
    #: Pending-order expiry, seconds from issue (0/absent =
    #: good-til-cancelled). Refused above COMMAND_TTL_MAX_SECS.
    expiry_secs: int | None = None
    comment: str = "NSE_OPEN"

    @classmethod
    def from_operation(cls, operation: Mapping[str, Any]) -> V2OpenOrderRequest:
        """Build a request from a Platform v2 ``operation`` object (§14.2).

        Accepts the Platform wire names ``order_type`` / ``stop_limit``
        as aliases of the Bridge-native ``order_kind`` / ``stoplimit``
        (Bridge name wins when both are present). A pending kind sent
        under either name is preserved — it must never silently fall
        back to ``"market"``. A missing kind fails loudly (audit
        P2-3: no market default — a dropped kind field must never
        turn a pending order into a market order). Unknown kinds,
        unknown sides, missing required fields, a pending kind
        without a price, a ``stop_limit`` kind without a
        ``stop_limit`` price, a negative deviation, an out-of-range
        expiry, and non-positive SL/TP fail loudly here, never at
        dispatch.
        """
        if not isinstance(operation, Mapping):
            raise TypeError("v2 open_order operation is not a JSON object")
        op_type = operation.get("type")
        if op_type is not None and str(op_type) != "open_order":
            raise ValueError(f"v2 open_order operation has unexpected type {op_type!r}")
        kind_raw = operation.get("order_kind")
        if kind_raw in (None, ""):
            kind_raw = operation.get("order_type")
        if kind_raw in (None, ""):
            raise ValueError(
                "v2 open_order operation is missing 'order_kind' (or 'order_type'); "
                "an explicit order kind (market | limit | stop | stop_limit) is required"
            )
        order_kind = str(kind_raw).strip().lower()
        if order_kind not in ORDER_KINDS:
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
        if volume <= 0:
            raise ValueError("v2 open_order requires volume > 0")
        sl_value = _num(operation.get("sl"), "sl")
        tp_value = _num(operation.get("tp"), "tp")
        if sl_value is not None and sl_value <= 0:
            raise ValueError("v2 open_order sl must be > 0")
        if tp_value is not None and tp_value <= 0:
            raise ValueError("v2 open_order tp must be > 0")
        price = _num(operation.get("price"), "price")
        if order_kind in ("limit", "stop", "stop_limit") and (price is None or price <= 0):
            raise ValueError(f"v2 open_order order_kind={order_kind} requires price > 0")
        stoplimit = _num(stoplimit_raw, "stoplimit")
        if order_kind == "stop_limit" and (stoplimit is None or stoplimit <= 0):
            raise ValueError("v2 open_order order_kind=stop_limit requires stop_limit > 0")
        if order_kind == "market" and price is not None:
            # A price on a market entry is forbidden by §14.2
            # (ignored server-side); the Bridge refuses rather than
            # silently ignoring a field the sender thought mattered.
            raise ValueError("v2 open_order order_kind=market forbids price")
        deviation_raw = operation.get("deviation")
        deviation = 20
        if deviation_raw is not None:
            deviation_num = _num(deviation_raw, "deviation")
            if deviation_num is None or deviation_num < 0 or deviation_num > DEVIATION_MAX:
                raise ValueError(f"v2 open_order deviation must be 0..{DEVIATION_MAX}")
            if float(deviation_num) != int(deviation_num):
                raise ValueError("v2 open_order deviation must be a whole number of points")
            deviation = int(deviation_num)
        expiry_raw = operation.get("expiry_secs")
        expiry_secs: int | None = None
        if expiry_raw is not None:
            expiry_num = _num(expiry_raw, "expiry_secs")
            if expiry_num is None or expiry_num < 0 or expiry_num > COMMAND_TTL_MAX_SECS:
                raise ValueError(f"v2 open_order expiry_secs must be 0..{COMMAND_TTL_MAX_SECS}")
            if float(expiry_num) != int(expiry_num):
                raise ValueError("v2 open_order expiry_secs must be a whole number of seconds")
            expiry_secs = int(expiry_num)
        comment = operation.get("comment")
        return cls(
            symbol=str(symbol).strip(),
            side=cast(Literal["buy", "sell"], side),
            volume=volume,
            sl=sl_value if sl_value is not None else 0.0,
            tp=tp_value if tp_value is not None else 0.0,
            order_kind=cast(Literal["market", "limit", "stop", "stop_limit"], order_kind),
            price=price,
            stoplimit=stoplimit,
            deviation=deviation,
            expiry_secs=expiry_secs,
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
        logger.warning("[V2_OPEN_ORDER] refused — v2 flag is off (opted out via NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0)")
        raise BridgeError(
            ErrorCode.DISABLED,
            "v2 open_order is DISABLED: the NSE_BRIDGE_ENABLE_V2_OPEN_ORDER flag is off "
            "(opted out via NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0). "
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
    if negotiation.schema_version is not None and negotiation.schema_version != V2_SCHEMA_VERSION:
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

    Pre-validates the request against live MT5 state (symbol,
    volume, SL/TP), then dispatches through the proven local
    primitives (:func:`market_order` /
    :func:`trade_send_pending_order`), which carry the mandatory
    pre-send ``order_check`` and W4 reconciliation.
    """
    check_v2_open_order_available(flag_enabled=flag_enabled, negotiation=negotiation)
    if request.volume <= 0:
        raise BridgeError(
            ErrorCode.VALIDATION,
            "v2 open_order requires a positive volume.",
            http_status=422,
            details={"volume": request.volume},
        )
    # MT5 pre-flight: symbol / volume / SL-TP geometry against the
    # live tick, before anything is sent. Pending kinds reuse the
    # structural validator inside trade_send_pending_order; market
    # kinds get the same checks here.
    if request.order_kind == "market":
        reasons = validate_market_open_params(
            ctx,
            symbol=request.symbol,
            side=request.side,
            volume=request.volume,
            sl=request.sl,
            tp=request.tp,
        )
        if reasons:
            raise BridgeError(
                ErrorCode.VALIDATION,
                f"Structurally invalid market open: {', '.join(reasons)}",
                http_status=422,
                details={"reasons": reasons, "symbol": request.symbol},
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
            deviation=request.deviation,
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
    expiration_utc: str | None = None
    if request.expiry_secs:
        expiration_utc = (datetime.now(UTC) + timedelta(seconds=request.expiry_secs)).isoformat().replace("+00:00", "Z")
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
            expiration_utc=expiration_utc,
            comment=request.comment,
        ),
    )

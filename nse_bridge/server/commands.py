"""Command path (CONTRACT v1 §6) — server → Bridge → MT5.

Envelope (§6.2 — the whole JSON frame; ``"type": "command"`` sits
alongside it)::

    {
      "type": "command",
      "command_id": "<uuidv7, unique per command>",
      "account_id": "12345678901",          # MT5 login as a STRING (§7.1)
      "device_id": "<device uuid>",
      "operation": {"type": "close_position", "ticket": "9000001"},
      "issued_at": "2026-11-16T00:00:00Z",
      "expires_at": "2026-11-16T01:00:00Z",
      "idempotency_key": "<== command_id in v1>"
    }

``operation`` is a tagged object: ``close_position`` takes
``{type, ticket}`` — plus, as a PROPOSED Phase 2b amendment (PR body,
Platform decision outstanding), an optional ``volume`` field for the
reduce-volume operation (CONTRACTS.md §7 ``partial_close`` row):
``{type, ticket, volume?}`` with ``volume`` a positive number of
lots; absent/``null`` volume keeps the v1 semantics (full close), so
a server that never sends the field behaves exactly as before.
``modify_sl_tp`` / ``modify_pending`` take ``{type, ticket, sl?, tp?}``
with ``sl``/``tp`` nullable (absent = leave unchanged). ``ticket`` is
a string (MT5 ticket ids exceed 32-bit ints). Unknown fields are
ignored. There is no per-command signature in v1 (§6.2, delta
D-CMD-3): commands are authenticated by the mutually-authenticated
WSS session itself.

Server allowlist (§6.1) — EXACTLY three operations in v1:
``close_position``, ``modify_sl_tp``, ``modify_pending``. ``open_order``
and ``cancel_order`` are explicitly NOT sendable under a v1 envelope
(§6.1, §17.5 open decision); under a negotiated v2 envelope they are
sendable subject to the §17.5 activation gate (step 5b), whose flag
now defaults ON (opt-out: ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0``).
The full local 91-command registry stays intact — only the
SERVER channel enforces this allowlist, at this boundary.

``modify_pending`` capability gate (§17.4, step 5a of §6.3): the op is
contract-defined but NOT sendable until its capability gate passes —
(a) a ``ModifyPending`` variant + allowlist entry in the Connector,
(b) tests, (c) live proof at market open that ``trade_modify_sl_tp``
accepts a pending-order ticket, (d) a published minimum
``connector_version`` recorded in the contract. This Bridge has (a)
and (b); (c) is DONE (2026-11-19, live on the MetaQuotes-Demo
terminal, E-P2B-6 in docs/upgrade/PHASE-2B-VERIFICATION.md); (d) is
the Platform owner's decision and remains outstanding — so the gate
starts CLOSED: ``modify_pending`` is rejected as
``UnsupportedOperation`` exactly like any other non-allowlisted op
until (d) lands and ``modify_pending_proven=True`` is set by the code
that owns the decision. The server MUST NOT send it before then
either (its own capability rule, §17.4).

Local validation order (§6.3) — EXACT, the server assumes this order
when interpreting rejections (malformed → expired → wrong-device →
wrong-account → allowlist → entitlement → replay; only a SUCCESSFUL
validation consumes the replay id):

1. Envelope parses; ``expires_at`` is RFC 3339 → ``Malformed``
2. ``expires_at`` > now (UTC) → ``Expired`` (default 5 min from
   ``issued_at``, max 15 min)
3. ``device_id`` == this device → ``WrongDevice``
4. ``account_id`` ∈ authorized accounts for this device → ``WrongAccount``
5. ``operation.type`` ∈ allowlist → ``UnsupportedOperation``
   (5a: ``modify_pending`` additionally requires the §17.4 gate;
   5b: ``open_order`` additionally requires the §17.5 gate — the
   flag, default ON, plus a negotiated v2 envelope)
6. Feature enabled locally (``position_manager`` / ``auto_trade``
   entitlements from ``validate-key``) → ``AutoTradeNotEnabled``
7. ``command_id`` not in the bounded replay cache (10,000 ids) → ``Replayed``

Rejections return ``action.result`` with ``status: "rejected"`` + the
reason (§6.5). The Bridge MUST NOT execute a command that fails any
step, and MUST NOT retry a rejected command on its own.

Lifecycle (exact states, clarification #2 + spec §7)::

    RECEIVED → VALIDATED → AUTHORIZED → DISPATCHED → EXECUTING →
    COMPLETED | PARTIALLY_COMPLETED | FAILED | UNKNOWN | EXPIRED

``AUTHORIZED`` = entitlement + server authorization confirmed locally.
``PARTIALLY_COMPLETED`` = the broker executed part of the request and
the Bridge proved the resulting state against live MT5 truth (partial
close / DONE_PARTIAL retcode 10010). It is a LOCAL state: contract
§6.5 ``action.result`` has no partial status (dependency D5), so the
wire status stays ``executed`` — the only truthful §6.5 status for a
request that did execute — with the partial facts (``partial: true``,
``closed_volume``, ``remaining_volume``, ``reconciled``) carried in
the payload, in the local result store, and in telemetry. A first-class
wire representation awaits a contract amendment (D5); nothing is
invented here.
``UNKNOWN`` = result uncertain after timeout/disconnect — reconcile
the actual MT5 state before any retry; never blind-retry a
non-idempotent op. Validation rejections end the local lifecycle in
``FAILED`` (wire truth is the ``action.result`` status ``rejected``);
an expiry rejection ends it in ``EXPIRED``. The server-side lifecycle
(§6.6: ``pending → delivered → (executed | rejected | mcp_failed) |
expired``) is the Platform's; the Bridge maps its local states onto
``action.result`` truthfully.

Results (§6.5): ``MessageEnvelope`` with ``event_type:
"action.result"``, ``protocol_version: "1.0"``, ``schema_version:
"2026-11-17"``, ``message_id`` uuidv7, ``correlation_id`` =
command_id, status exactly ``rejected | executed | mcp_failed``,
``started_at``/``completed_at``/``duration_ms`` measured client-side,
raw MT5 payload preserved, ``error`` object only on failure with a
``retry_safe`` flag. If the socket is down, results persist in the
bounded pending queue and are delivered on reconnect — never dropped
silently. Round-trip invariant: every issued command reaches exactly
one terminal state; a command never executes twice.

Mapping onto the Python driver (NOT the MCP transport — §8 describes
the Rust Connector's transport; this Bridge keeps its working Python
driver, per ARCHITECTURE-CONTRACT §5):

- ``close_position`` → :func:`nse_bridge.engine.trading.close_position`
  (close-by-ticket; idempotent ambiguous-close handling kept).
- ``modify_sl_tp`` → :func:`nse_bridge.engine.trading.modify_position_sltp`
  (SL/TP by ticket; absent sl/tp filled from the live position).
- ``modify_pending`` → :func:`nse_bridge.engine.trading.modify_pending_order_sltp`
  (SL/TP on a pending order by ticket; SL/TP only — price/volume/
  expiration changes have no verified action, §17.4).

Existing execution hardening is kept: stops/freeze checks, filling-mode
resolution, retcode map, ``TRADE_UNVERIFIED`` on ambiguous results,
close idempotency, never blind-retry after an uncertain result.

``error.category`` note: §6.5's ``error`` object carries a
``category`` from the Connector's error catalog (``ERROR_CATALOG.md``,
e.g. ``Prerequisite``) — that catalog is Platform-side and NOT part of
CONTRACT v1, so the categories used here (``Validation``,
``Execution``, ``Prerequisite``, ``Unknown``) are bridge-local
approximations, documented as such. ``retry_safe`` is False for every
financial op in this phase: the server must re-authorize with a NEW
``command_id`` rather than rely on a blind client retry.

P3 v2 protocol (Bridge v1.4.0 B3 — Platform MASTER §14,
``connector::contract`` on Platform origin/main):

- Versioned envelopes: the frame carries ``protocol_version``
  (``"1"`` | ``"2"``) and ``priority`` (0–9, CLOSE 9 / CANCEL 8).
  ``"2"`` is honoured only when this Bridge speaks v2 (version >=
  1.3.0); absent or unknown negotiates down to v1, fail-closed.
- v2 ops: ``open_order`` (market + pending entry, §14.2 fields
  ``symbol`` / ``side`` / ``order_type`` / ``volume`` / ``price`` /
  ``stop_limit`` / ``deviation`` / ``expiry_secs`` + provenance
  refs), ``cancel_order``, ``partial_close`` — plus the v1 ops
  unchanged. ``trailing_stop`` / ``break_even`` stay refused: their
  MT5 primitives live in the (dormant) policy service, not on this
  path — a v2 op with no verified primitive is never guessed.
- ``open_order`` additionally requires BOTH v2 gates: the flag
  (``enable_v2_open_order``, default ON — opt-out via
  ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0``) AND a negotiated v2 envelope.
  The execution path runs through
  :mod:`nse_bridge.engine.v2_open_order`, which pre-validates
  against live MT5 state and dispatches through the proven market /
  pending primitives (mandatory ``order_check``, W4 reconciliation).
  A pending kind sent under the Platform wire names is never
  silently executed as market (PR #20 lesson).
- Idempotency: terminal ``action.result`` frames are stored by
  idempotency key (``idempotency_key`` or ``command_id``). A
  redelivery — same ``command_id`` (validator replay cache) or the
  same idempotency key under a new ``command_id`` — returns the
  stored result with ``deduplicated: true`` and never touches MT5
  twice.
- Fencing: a single active command per (device, fence token), where
  the token is the envelope's ``policy_revision`` when present
  (``"default"`` otherwise). A second command waits for the fence
  (bounded); a command whose ``policy_revision`` differs from the
  executor's expected revision (when configured — the server is
  the authority by default) is rejected as ``Fenced``.
- Server lifecycle (Platform §14.4 mapping): every command records
  ``pending → delivered → acknowledged → confirmed | failed`` with
  timestamps (``ServerLifecycle``), carried on the result envelope.
  Dispatch is bounded by ``execution_timeout_s`` (default 120 s):
  a dispatch that overruns it is reconciled against live MT5 truth
  before any outcome is reported — a late success that cannot be
  proven is UNKNOWN, never a blind success.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import Condition, Lock
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..engine.v2_open_order import negotiate_protocol_version
from ..errors import BridgeError, ErrorCode
from ..timeutil import normalize_utc
from .queue import BoundedPendingQueue
from .rest import Entitlements
from .uuid7 import new_uuid7_str

if TYPE_CHECKING:
    from ..engine.common import BridgeContext

__all__ = [
    "MODIFY_PENDING_GATE_NOTE",
    "PROTOCOL_VERSION_V1",
    "PROTOCOL_VERSION_V2",
    "REPLAY_CACHE_MAX",
    "SERVER_ALLOWLIST",
    "SERVER_ALLOWLIST_V2",
    "V2_ONLY_OPERATIONS",
    "CommandEnvelope",
    "CommandExecution",
    "CommandExecutor",
    "CommandFence",
    "CommandState",
    "CommandValidator",
    "IdempotencyStore",
    "RejectionReason",
    "ReplayCache",
    "ServerLifecycle",
    "ValidationOutcome",
    "build_action_result",
    "default_priority",
    "rejection_result",
]

PROTOCOL_VERSION = "1.0"
SCHEMA_VERSION = "2026-11-17"
REPLAY_CACHE_MAX = 10_000
IDEMPOTENCY_STORE_MAX = 10_000

#: P3 v2 protocol versions (§14.1). Unknown/unnegotiated = "1"
#: (fail-closed to the old contract).
PROTOCOL_VERSION_V1 = "1"
PROTOCOL_VERSION_V2 = "2"

#: §6.1 — the v1 server allowlist. Anything else is rejected as
#: UnsupportedOperation before it ever reaches MT5.
SERVER_ALLOWLIST = frozenset({"close_position", "modify_sl_tp", "modify_pending"})

#: §14.1 — the v2 server allowlist: every v1 op plus the new typed
#: ops this Bridge can execute against verified primitives.
#: ``trailing_stop`` / ``break_even`` are deliberately NOT here:
#: their execution lives in the (dormant) policy service and has no
#: verified command-path primitive in this slice — a v2 op with no
#: primitive is refused, never guessed.
SERVER_ALLOWLIST_V2 = frozenset(
    {"close_position", "partial_close", "modify_sl_tp", "modify_pending", "open_order", "cancel_order"}
)

#: §14.1 — operations that exist ONLY in v2 (refused under v1).
V2_ONLY_OPERATIONS = frozenset({"open_order", "cancel_order", "partial_close", "trailing_stop", "break_even"})

#: Dispatch priority (§14): CLOSE is the risk-reducing action, so
#: it jumps the queue; ``cancel_order`` is next. Range 0–9.
PRIORITY_DEFAULT: int = 0
PRIORITY_CLOSE: int = 9
PRIORITY_CANCEL: int = 8


def default_priority(kind: str) -> int:
    """Default dispatch priority for an operation kind (§14)."""
    if kind in ("close_position", "partial_close"):
        return PRIORITY_CLOSE
    if kind == "cancel_order":
        return PRIORITY_CANCEL
    return PRIORITY_DEFAULT


#: §17.4 close-out status. (a) allowlist code exists in this module;
#: (b) tests exist in tests/unit/test_server_commands.py; (c) live
#: proof at market open that TRADE_ACTION_MODIFY accepts a
#: pending-order ticket: DONE 2026-11-19 on the MetaQuotes-Demo
#: terminal (retcode 10009, SL landed on the live order, order_check
#: passed — E-P2B-6 in docs/upgrade/PHASE-2B-VERIFICATION.md); (d) NO
#: published minimum connector_version is recorded in the contract —
#: that publication is the Platform owner's decision. The gate stays
#: CLOSED until (d) lands.
MODIFY_PENDING_GATE_NOTE = (
    "modify_pending is capability-gated (§17.4): conditions (a) code, "
    "(b) tests and (c) live proof (2026-11-19, E-P2B-6) are done; (d) "
    "the published minimum connector_version is the Platform owner's "
    "decision and is still outstanding, so modify_pending is rejected "
    "as UnsupportedOperation until the version gate is published."
)

#: Local MT5 source-tool names (§6.5 ``source_tool``) per operation.
SOURCE_TOOL = {
    "close_position": "trade_close_single_position",
    "partial_close": "trade_close_single_position",
    "modify_sl_tp": "trade_modify_sl_tp",
    "modify_pending": "trade_modify_sl_tp",
    "cancel_order": "trade_cancel_pending_order",
    "open_order": "trade_send_market_order",  # pending opens report trade_send_pending_order
}


class RejectionReason(str, Enum):
    MALFORMED = "Malformed"
    EXPIRED = "Expired"
    WRONG_DEVICE = "WrongDevice"
    WRONG_ACCOUNT = "WrongAccount"
    UNSUPPORTED_OPERATION = "UnsupportedOperation"
    AUTO_TRADE_NOT_ENABLED = "AutoTradeNotEnabled"
    REPLAYED = "Replayed"
    #: P3 v2 fencing: another command holds the per-device fence,
    #: or the envelope's policy_revision is superseded. The server
    #: must re-issue with a NEW command_id — never a blind retry.
    FENCED = "Fenced"


class CommandState(str, Enum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


#: Legal lifecycle transitions (clarification #2 + spec §7). Terminal states
#: (COMPLETED / PARTIALLY_COMPLETED / FAILED / UNKNOWN / EXPIRED) have no
#: outgoing edges. AUTHORIZED → EXPIRED is legal: the dispatch re-check
#: (§6.2/§6.6) may expire a command that expired while queued — without
#: this edge the re-check raises instead of ending the command EXPIRED.
LEGAL_TRANSITIONS: dict[CommandState, frozenset[CommandState]] = {
    CommandState.RECEIVED: frozenset({CommandState.VALIDATED, CommandState.FAILED, CommandState.EXPIRED}),
    CommandState.VALIDATED: frozenset({CommandState.AUTHORIZED, CommandState.FAILED}),
    CommandState.AUTHORIZED: frozenset({CommandState.DISPATCHED, CommandState.FAILED, CommandState.EXPIRED}),
    CommandState.DISPATCHED: frozenset({CommandState.EXECUTING, CommandState.FAILED, CommandState.EXPIRED}),
    CommandState.EXECUTING: frozenset(
        {CommandState.COMPLETED, CommandState.PARTIALLY_COMPLETED, CommandState.FAILED, CommandState.UNKNOWN}
    ),
    CommandState.COMPLETED: frozenset(),
    CommandState.PARTIALLY_COMPLETED: frozenset(),
    CommandState.FAILED: frozenset(),
    CommandState.UNKNOWN: frozenset(),
    CommandState.EXPIRED: frozenset(),
}


@dataclass
class CommandEnvelope:
    """A parsed §6.2 command envelope."""

    command_id: str
    account_id: str  # MT5 login as a string (§7.1)
    device_id: str
    operation_type: str
    ticket: str
    sl: float | None = None
    tp: float | None = None
    #: Optional reduce-volume close volume (PROPOSED Phase 2b wire
    #: amendment, close_position only; absent/None = full close, v1
    #: semantics). Validated at parse time (positive number) and again
    #: against the live position volume at execution.
    volume: float | None = None
    #: W5 fields (PROPOSED §17.5 wire shapes — the market form follows
    #: CONTRACTS.md §7; the pending form is proposed in the PR body).
    #: Parsed only when ``operation.type == "open_order"``; the §17.5
    #: gate (step 5b — flag, default on, plus a negotiated v2
    #: envelope) still stands between these fields and MT5.
    symbol: str | None = None
    side: str | None = None
    order_kind: str | None = None  # market | limit | stop | stop_limit
    price: float | None = None  # pending entries only
    stoplimit: float | None = None  # stop_limit entries only
    #: P3 v2 envelope fields (§14.1–§14.2). ``protocol_version`` is
    #: "1" | "2" (absent/unknown → "1", fail-closed); ``priority``
    #: is 0–9 (clamped). The remaining fields are parsed for every
    #: frame and meaningful only for the ops that carry them.
    protocol_version: str = PROTOCOL_VERSION_V1
    priority: int = PRIORITY_DEFAULT
    deviation: float | None = None  # market opens: MT5 points, >= 0
    expiry_secs: int | None = None  # pending opens: 0..900
    comment: str | None = None
    policy_revision: int | None = None  # §8.6 fencing token
    strategy_ref: str | None = None
    decision_ref: str | None = None
    risk_ref: str | None = None
    approval_ref: str | None = None
    trailing_distance: float | None = None  # trailing_stop only
    break_even_offset: float | None = None  # break_even only
    issued_at: str | None = None
    expires_at: str | None = None
    idempotency_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ticket_int(self) -> int:
        return int(self.ticket)


def _ticket_str(ticket: Any) -> str:
    """Normalize an operation ticket to a numeric string (§6.2).

    A non-integral float must be rejected, never truncated to a
    different ticket (Codex P2: 9000001.5 silently became 9000001).
    """
    if isinstance(ticket, bool):
        raise TypeError("command operation ticket is not an integer")
    if isinstance(ticket, float):
        if not ticket.is_integer():
            raise ValueError("command operation ticket is not an integer")
        return str(int(ticket))
    if isinstance(ticket, int):
        return str(ticket)
    text = str(ticket).strip()
    try:
        int(text)  # ticket must be numeric
    except (TypeError, ValueError):
        raise ValueError("command operation ticket is not numeric") from None
    return text


def parse_envelope(frame: Any) -> CommandEnvelope:
    """Parse a ``command`` frame into an envelope or raise Malformed.

    Step 1 of §6.3: the envelope parses and ``expires_at`` is RFC 3339.
    """
    if not isinstance(frame, dict):
        raise TypeError("command frame is not a JSON object")
    for key in ("command_id", "account_id", "device_id", "operation", "issued_at", "expires_at"):
        if frame.get(key) in (None, ""):
            raise ValueError(f"command frame is missing {key!r}")
    operation = frame.get("operation")
    if not isinstance(operation, dict) or not operation.get("type"):
        raise ValueError("command frame operation is missing or has no type")
    op_type = str(operation["type"])
    ticket = operation.get("ticket")
    if op_type == "open_order":
        # W5: an open has no broker ticket yet — the ticket is assigned
        # by MT5 on execution. The frame must still be a well-formed
        # open (structurally validated at dispatch, inside the gate).
        ticket_str = ""
        if ticket not in (None, ""):
            ticket_str = _ticket_str(ticket)
    else:
        if ticket in (None, ""):
            raise ValueError("command operation is missing ticket")
        ticket_str = _ticket_str(ticket)
    expires = normalize_utc(frame.get("expires_at"), 0)
    if expires is None:
        raise ValueError("command expires_at is not RFC 3339")
    if normalize_utc(frame.get("issued_at"), 0) is None:
        raise ValueError("command issued_at is not RFC 3339")

    def _num(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("command sl/tp is not a number") from None

    volume = _num(operation.get("volume"))
    if volume is not None and volume <= 0:
        # A non-positive reduce-volume is a shape violation, not a
        # broker state issue: reject at parse time (§6.3 step 1).
        raise ValueError("command volume must be a positive number of lots")

    # W5 open-order fields (parsed for every frame; meaningful only for
    # operation.type == "open_order"). Structural requirements are
    # enforced at dispatch, inside the gate.
    # P3a wire compat (§14.2): the Platform emits the v2 names
    # `order_type` / `stop_limit`; the Bridge's native names are
    # `order_kind` / `stoplimit`. Accept both — Bridge name wins when
    # both are present. A pending entry sent under the Platform names
    # must never silently default to `market`.
    order_kind_raw = operation.get("order_kind")
    if order_kind_raw in (None, ""):
        order_kind_raw = operation.get("order_type")
    order_kind = str(order_kind_raw).strip().lower() if order_kind_raw not in (None, "") else None
    if order_kind == "market" or (operation.get("type") == "open_order" and order_kind is None):
        order_kind = order_kind or "market"  # CONTRACTS §7 market default
    stoplimit_raw = operation.get("stoplimit")
    if stoplimit_raw is None:
        stoplimit_raw = operation.get("stop_limit")

    # P3 v2 envelope fields (§14.1–§14.2). Unknown protocol versions
    # negotiate down to v1 (fail-closed); a present-but-unknown
    # order kind is NOT normalised here — the v2 seam
    # (``V2OpenOrderRequest.from_operation``) refuses it loudly at
    # dispatch, so a pending kind can never silently become market.
    protocol_version = frame.get("protocol_version")
    if protocol_version in (None, ""):
        protocol_version = PROTOCOL_VERSION_V1
    else:
        protocol_version = str(protocol_version)
        if protocol_version not in (PROTOCOL_VERSION_V1, PROTOCOL_VERSION_V2):
            logger.warning("[COMMAND] unknown protocol_version {!r} — treating as v1 (fail-closed)", protocol_version)
            protocol_version = PROTOCOL_VERSION_V1
    priority_raw = frame.get("priority")
    if priority_raw is None:
        priority = default_priority(op_type)
    else:
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError):
            raise ValueError("command priority is not an integer") from None
        priority = max(0, min(9, priority))  # Platform clamps 0–9

    def _opt_num(value: Any, name: str) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"command {name} is not a number") from None

    def _opt_int(value: Any, name: str) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"command {name} is not an integer") from None

    deviation = _opt_num(operation.get("deviation"), "deviation")
    if deviation is not None and deviation < 0:
        raise ValueError("command deviation must be >= 0")
    expiry_secs = _opt_int(operation.get("expiry_secs"), "expiry_secs")
    if expiry_secs is not None and not 0 <= expiry_secs <= 900:
        raise ValueError("command expiry_secs must be 0..900")
    policy_revision = _opt_int(operation.get("policy_revision"), "policy_revision")
    trailing_distance = _opt_num(operation.get("trailing_distance"), "trailing_distance")
    break_even_offset = _opt_num(operation.get("break_even_offset"), "break_even_offset")

    def _opt_str(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    return CommandEnvelope(
        command_id=str(frame["command_id"]),
        account_id=str(frame["account_id"]),
        device_id=str(frame["device_id"]),
        operation_type=str(operation["type"]),
        ticket=ticket_str,
        sl=_num(operation.get("sl")),
        tp=_num(operation.get("tp")),
        volume=volume,
        symbol=str(operation["symbol"]).strip() if operation.get("symbol") not in (None, "") else None,
        side=str(operation["side"]).strip().lower() if operation.get("side") not in (None, "") else None,
        order_kind=order_kind,
        price=_num(operation.get("price")),
        stoplimit=_num(stoplimit_raw),
        protocol_version=protocol_version,
        priority=priority,
        deviation=deviation,
        expiry_secs=expiry_secs,
        comment=_opt_str(operation.get("comment")),
        policy_revision=policy_revision,
        strategy_ref=_opt_str(operation.get("strategy_ref")),
        decision_ref=_opt_str(operation.get("decision_ref")),
        risk_ref=_opt_str(operation.get("risk_ref")),
        approval_ref=_opt_str(operation.get("approval_ref")),
        trailing_distance=trailing_distance,
        break_even_offset=break_even_offset,
        issued_at=str(frame["issued_at"]),
        expires_at=str(frame["expires_at"]),
        idempotency_key=str(frame["idempotency_key"]) if frame.get("idempotency_key") is not None else None,
        raw=frame,
    )


class ReplayCache:
    """Bounded replay cache (10,000 ids, §6.3 step 7).

    Only a SUCCESSFUL validation consumes an id. Failed validations
    leave the id reusable (e.g. an expired command re-issued with a
    fresh ``expires_at`` under the same id is not a replay).
    """

    def __init__(self, max_ids: int = REPLAY_CACHE_MAX) -> None:
        self.max_ids = max_ids
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, command_id: str) -> bool:
        return command_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def consume(self, command_id: str) -> None:
        self._ids[command_id] = None
        while len(self._ids) > self.max_ids:
            self._ids.popitem(last=False)


class IdempotencyStore:
    """Bounded store of terminal ``action.result`` frames, keyed by
    idempotency key (P3 v2 §14.5).

    The key is the envelope's ``idempotency_key`` (``command_id`` in
    v1, and the Platform's replay sets it to ``command_id`` — a
    distinct key is accepted for forward compatibility). A replay —
    the same key under the same or a new ``command_id`` — returns
    the stored terminal result instead of executing again. Only
    TERMINAL results are stored; validation rejections never land
    here (a rejected command was never executed, so a re-issue must
    re-validate). Stored results are returned as copies with
    ``deduplicated: true`` stamped on top, so the server can tell a
    replayed answer from a fresh execution.
    """

    def __init__(self, max_keys: int = IDEMPOTENCY_STORE_MAX) -> None:
        self.max_keys = max_keys
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._results

    def __len__(self) -> int:
        return len(self._results)

    def get(self, key: str) -> dict[str, Any] | None:
        stored = self._results.get(key)
        if stored is None:
            return None
        copy = dict(stored)
        copy["deduplicated"] = True
        return copy

    def put(self, key: str, result: dict[str, Any]) -> None:
        self._results[key] = dict(result)
        while len(self._results) > self.max_keys:
            self._results.popitem(last=False)


class CommandFence:
    """Single active command per (device, fence token) (P3 v2 §8.6).

    The token is the envelope's ``policy_revision`` when present
    (``"default"`` otherwise): a policy change fences the old
    revision's commands out — they wait for the fence only if they
    share the token, and a superseded revision is rejected by the
    executor's policy check instead of executing under stale
    authorization. ``acquire`` blocks up to ``timeout_s``; on
    timeout it returns False and the caller rejects as ``Fenced``
    (the server re-issues with a new ``command_id``). The fence is
    always released, even when dispatch crashes — a stuck fence
    would wedge the device's whole command path.
    """

    def __init__(self) -> None:
        self._cond = Condition()
        self._active: dict[tuple[str, str], str] = {}

    @staticmethod
    def token_for(envelope: CommandEnvelope) -> str:
        return str(envelope.policy_revision) if envelope.policy_revision is not None else "default"

    def acquire(self, device_id: str, token: str, command_id: str, timeout_s: float) -> bool:
        key = (device_id, token)
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            while key in self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            self._active[key] = command_id
            return True

    def release(self, device_id: str, token: str, command_id: str) -> None:
        key = (device_id, token)
        with self._cond:
            if self._active.get(key) == command_id:
                del self._active[key]
            self._cond.notify_all()

    def active_command(self, device_id: str, token: str) -> str | None:
        with self._cond:
            return self._active.get((device_id, token))


#: Server-side lifecycle states (Platform §14.4) and the local
#: state each one is recorded at.
SERVER_LIFECYCLE_STATES = ("pending", "delivered", "acknowledged", "confirmed", "failed", "unknown", "expired")


@dataclass
class ServerLifecycle:
    """Per-command record of the Platform §14.4 lifecycle.

    Local mapping: ``pending`` at receipt, ``delivered`` at
    successful validation, ``acknowledged`` at dispatch start,
    ``confirmed`` on executed (incl. partial), ``failed`` on
    rejected / failed / expired, ``unknown`` when the outcome could
    not be proven against MT5 truth. The wire ``action.result`` is
    still the acknowledgement the server reads; this record is the
    Bridge's own audit trail, carried on the result envelope.
    """

    command_id: str
    states: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.record("pending")

    def record(self, state: str) -> None:
        if state not in SERVER_LIFECYCLE_STATES:
            raise ValueError(f"unknown server lifecycle state {state!r}")
        self.states.append((state, datetime.now(UTC).isoformat()))

    @property
    def current(self) -> str:
        return self.states[-1][0] if self.states else "pending"

    def to_dict(self) -> dict[str, Any]:
        return {"command_id": self.command_id, "current": self.current, "states": [list(s) for s in self.states]}


@dataclass
class ValidationOutcome:
    envelope: CommandEnvelope | None = None
    rejection: RejectionReason | None = None

    @property
    def ok(self) -> bool:
        return self.envelope is not None and self.rejection is None


class CommandValidator:
    """§6.3 local validation, in the exact contract order.

    ``get_account_id`` returns the currently bound MT5 login as a
    string, or None when no account is bound (fail closed: any command
    is then WrongAccount). ``entitlements`` is the server-resolved
    ``validate-key`` result (secure default: everything disabled); it
    may be a callable so re-validation picks up fresh entitlements.
    """

    def __init__(
        self,
        device_id: str,
        *,
        get_account_id: Callable[[], str | None],
        entitlements: Entitlements | Callable[[], Entitlements] | None = None,
        replay_cache: ReplayCache | None = None,
        modify_pending_proven: bool = False,
        open_order_authorized: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.device_id = device_id
        self._get_account_id = get_account_id
        self._entitlements_source = entitlements
        self.replay_cache = replay_cache or ReplayCache()
        self.modify_pending_proven = modify_pending_proven
        #: P3 v2 / §17.5 activation gate, first half. The flag
        #: defaults ON at the settings layer
        #: (``BridgeSettings.enable_v2_open_order``; opt-out via
        #: ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0``) — this constructor
        #: parameter itself defaults False (fail-closed unless the
        #: caller passes the setting, as ``realtime/channel.py``
        #: does): ``open_order`` additionally requires a negotiated
        #: v2 envelope (``protocol_version: "2"`` — see step 5); the
        #: flag alone never opens it, and negotiation alone never
        #: opens it either. Both gates must be open before any open
        #: reaches MT5.
        self.open_order_authorized = open_order_authorized
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def entitlements(self) -> Entitlements:
        source = self._entitlements_source
        if callable(source):
            try:
                return source() or Entitlements()
            except Exception as exc:  # noqa: BLE001 — an entitlements source failure means "no entitlements"
                logger.debug("[COMMAND] entitlements source failed: {}", type(exc).__name__)
                return Entitlements()
        return source or Entitlements()

    def validate(self, frame: Any) -> ValidationOutcome:
        # 1 — malformed
        try:
            envelope = parse_envelope(frame)
        except (ValueError, TypeError) as exc:
            # TypeError: parse_envelope raises it for a non-dict frame
            # (R10) — a malformed frame is a rejection, never an escape.
            logger.warning("[COMMAND] malformed command frame: {}", exc)
            return ValidationOutcome(rejection=RejectionReason.MALFORMED)
        # 2 — expired
        expires = normalize_utc(envelope.expires_at, 0)
        assert expires is not None  # parse_envelope already checked
        if expires <= self._clock():
            return ValidationOutcome(rejection=RejectionReason.EXPIRED)
        # 3 — wrong device
        if envelope.device_id != self.device_id:
            return ValidationOutcome(rejection=RejectionReason.WRONG_DEVICE)
        # 4 — wrong account (fail closed when no account is bound)
        bound = self._get_account_id()
        if bound is None or envelope.account_id != bound:
            return ValidationOutcome(rejection=RejectionReason.WRONG_ACCOUNT)
        # 5 — allowlist (5a: modify_pending capability gate, §17.4;
        #     5b: v2 negotiation + open_order §17.5 activation gate).
        # P3 v2 (§14.1): the envelope's protocol_version negotiates
        # the contract — "2" is honoured only when this Bridge speaks
        # v2, anything else is v1, fail-closed. v2-only ops on a v1
        # negotiation are refused, and open_order additionally
        # requires the flag (default ON via settings; opt-out
        # ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0``) — the v2-negotiation
        # half stays fail-closed.
        negotiation = negotiate_protocol_version(envelope.protocol_version)
        allowed = SERVER_ALLOWLIST_V2 if negotiation.negotiated else SERVER_ALLOWLIST
        if envelope.operation_type not in allowed:
            if envelope.operation_type in V2_ONLY_OPERATIONS and not negotiation.negotiated:
                logger.warning(
                    "[COMMAND] {} requires protocol v2 (envelope protocol_version={!r})",
                    envelope.operation_type,
                    envelope.protocol_version,
                )
            return ValidationOutcome(rejection=RejectionReason.UNSUPPORTED_OPERATION)
        if envelope.operation_type == "open_order" and not self.open_order_authorized:
            logger.warning("[COMMAND] open_order refused — v2 flag is off (§17.5 activation gate)")
            return ValidationOutcome(rejection=RejectionReason.UNSUPPORTED_OPERATION)
        if envelope.operation_type == "modify_pending" and not self.modify_pending_proven:
            logger.warning("[COMMAND] modify_pending rejected — capability gate closed (§17.4)")
            return ValidationOutcome(rejection=RejectionReason.UNSUPPORTED_OPERATION)
        # 6 — entitlement (position_manager AND auto_trade, §6.4 step 2)
        if not (self.entitlements.position_manager and self.entitlements.auto_trade):
            return ValidationOutcome(rejection=RejectionReason.AUTO_TRADE_NOT_ENABLED)
        # 7 — replay (only a SUCCESSFUL validation consumes the id)
        if envelope.command_id in self.replay_cache:
            return ValidationOutcome(rejection=RejectionReason.REPLAYED)
        self.replay_cache.consume(envelope.command_id)
        return ValidationOutcome(envelope=envelope)


@dataclass
class CommandExecution:
    """One command's local lifecycle (clarification #2)."""

    command_id: str
    state: CommandState = CommandState.RECEIVED
    history: list[tuple[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.history.append((self.state.value, datetime.now(UTC).isoformat()))

    def transition(self, new_state: CommandState) -> None:
        if new_state not in LEGAL_TRANSITIONS[self.state]:
            raise ValueError(
                f"illegal lifecycle transition {self.state.value} → {new_state.value} for command {self.command_id}"
            )
        self.state = new_state
        self.history.append((new_state.value, datetime.now(UTC).isoformat()))


def _error_object(
    *,
    code: str,
    category: str,
    source_action: str,
    cause: str,
    raw: str | None = None,
    recovery: str | None = None,
    retry_safe: bool = False,
    server_intervention_required: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "category": category,
        "source_action": source_action,
        "raw": raw,
        "cause": cause,
        "recovery": recovery,
        "retry_safe": retry_safe,
        "server_intervention_required": server_intervention_required,
    }


def build_action_result(
    *,
    device_id: str,
    command_id: str,
    account_id: str,
    status: str,  # rejected | executed | mcp_failed (§6.5)
    source_tool: str,
    started_at: datetime,
    completed_at: datetime,
    payload: dict[str, Any] | None = None,
    user_id: str | None = None,
    error: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
    command_protocol_version: str | None = None,
    server_lifecycle: ServerLifecycle | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ``action.result`` MessageEnvelope (§6.5).

    ``duration_ms`` is measured client-side. The raw MT5 payload is
    preserved verbatim in ``payload`` / ``actual_mcp_result``. The
    ``error`` object is present only when status is ``rejected`` or
    ``mcp_failed``. ``command_protocol_version`` records the
    negotiated command contract ("1" | "2") — distinct from this
    envelope's own ``protocol_version: "1.0"`` result-envelope
    version. ``server_lifecycle`` carries the §14.4 lifecycle
    record when the executor has one.
    """
    duration_ms = max(0.0, (completed_at - started_at).total_seconds() * 1000.0)
    envelope: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "message_id": new_uuid7_str(),
        "correlation_id": command_id,
        "event_type": "action.result",
        "event_timestamp": completed_at.isoformat().replace("+00:00", "Z"),
        "device_id": device_id,
        "account_id": account_id,
        "source_tool": source_tool,
        "payload": payload or {},
        "data_quality": {
            "validation_warnings": validation_errors or [],
            "raw_available": bool(payload),
        },
        "status": status,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": round(duration_ms, 2),
        "actual_mcp_result": payload or {},
        "validation_errors": validation_errors or [],
        "execution_error": None,
    }
    if command_protocol_version is not None:
        envelope["command_protocol_version"] = command_protocol_version
    if server_lifecycle is not None:
        envelope["server_lifecycle"] = (
            server_lifecycle.to_dict() if isinstance(server_lifecycle, ServerLifecycle) else server_lifecycle
        )
    if user_id:
        envelope["user_id"] = user_id  # omit if unknown (§6.5)
    if error is not None:
        envelope["error"] = error
        envelope["execution_error"] = error
    return envelope


def rejection_result(
    *,
    device_id: str,
    command_id: str,
    account_id: str,
    reason: RejectionReason,
    operation_type: str,
    user_id: str | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    """``action.result`` with ``status: "rejected"`` for a §6.3 rejection."""
    now = datetime.now(UTC)
    started = started_at or now
    source_tool = SOURCE_TOOL.get(operation_type, operation_type)
    return build_action_result(
        device_id=device_id,
        command_id=command_id,
        account_id=account_id,
        status="rejected",
        source_tool=source_tool,
        started_at=started,
        completed_at=now,
        user_id=user_id,
        error=_error_object(
            code=reason.value,
            category="Validation",
            source_action=source_tool,
            cause=f"command rejected at local validation: {reason.value} (§6.3)",
            recovery="The server decides: re-issue with a new command_id only if the user re-authorizes.",
            retry_safe=False,
        ),
        validation_errors=[reason.value],
    )


class CommandExecutor:
    """Validates and executes §6.1 commands against the Python driver.

    Synchronous (runs on the MT5 worker thread via ``run_in_threadpool``
    at the call site). ``results_queue`` persists results across
    reconnects (§6.5) when provided.
    """

    def __init__(
        self,
        ctx: BridgeContext,
        validator: CommandValidator,
        *,
        results_queue: BoundedPendingQueue[dict[str, Any]] | None = None,
        user_id: str | None = None,
        telemetry: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        idempotency_store: IdempotencyStore | None = None,
        fence: CommandFence | None = None,
        expected_policy_revision: int | None = None,
        execution_timeout_s: float = 120.0,
        fence_timeout_s: float = 60.0,
    ) -> None:
        self.ctx = ctx
        self.validator = validator
        self.results_queue = results_queue
        self.user_id = user_id
        #: Clock for the dispatch expiry re-check (submit): injectable
        #: for tests (frozen clock), real wall clock in production.
        #: Mirrors CommandValidator's own clock (same default) — the
        #: re-check must agree with the validator, or a frame the
        #: validator accepted as fresh expires at dispatch.
        self._clock = clock or (lambda: datetime.now(UTC))
        # TelemetryEmitter (ARCHITECTURE-CONTRACT §8), optional: when
        # absent the executor still works, it just emits nothing.
        self.telemetry = telemetry if telemetry is not None else getattr(ctx, "telemetry", None)
        #: P3 v2 (§14.5): terminal results by idempotency key — a
        #: redelivery returns the stored result, never a second MT5
        #: execution. One store per executor (per device session).
        self.idempotency_store = idempotency_store or IdempotencyStore()
        #: P3 v2 (§8.6): single active command per (device, fence
        #: token). One fence per executor (per device session).
        self.fence = fence or CommandFence()
        #: Expected policy revision for the §8.6 fencing check.
        #: None (default) = the server is the authority; the Bridge
        #: enforces the check only when a revision is configured.
        self.expected_policy_revision = expected_policy_revision
        #: Max wall time for one dispatch (seconds). A dispatch that
        #: overruns it is reconciled against live MT5 truth before
        #: any outcome is reported (§14.4 UNKNOWN handling).
        self.execution_timeout_s = execution_timeout_s
        #: Max wait for the per-device fence (seconds). On expiry the
        #: command is rejected as Fenced — the server re-issues with
        #: a new command_id.
        self.fence_timeout_s = fence_timeout_s
        #: Bounded in-memory history of recently executed commands for
        #: the C4 Execution screen (spec §17): the TRUE local lifecycle
        #: state per command (incl. PARTIALLY_COMPLETED / UNKNOWN),
        #: newest last. In-memory only — the durable record is the
        #: ``action.result`` in ``results_queue`` (§6.5).
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        #: Serializes partial SL/TP updates (Codex P1): modify_sl_tp /
        #: modify_pending are read-modify-write (fetch live SL/TP, fill
        #: the unspecified side, send) — two concurrent updates for the
        #: same position interleave and one is lost. A single lock (not
        #: per-ticket): modify ops are rare and user-initiated, and no
        #: other lock is held here, so no deadlock.
        self._modify_lock = Lock()

    def recent_executions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent command executions, newest first (C4 GUI input)."""
        items = list(self._recent)[-max(0, limit) :] if limit > 0 else []
        return [dict(item) for item in reversed(items)]

    def _record_recent(self, frame: Any, execution: CommandExecution, result: dict[str, Any]) -> None:
        """Append one execution to the bounded recent history."""
        operation = ""
        if isinstance(frame, dict):
            operation = str((frame.get("operation") or {}).get("type") or "")
        payload_raw = result.get("payload")
        payload: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
        error_raw = result.get("error")
        error: dict[str, Any] = error_raw if isinstance(error_raw, dict) else {}
        self._recent.append(
            {
                "command_id": execution.command_id,
                "operation": operation,
                "state": execution.state.value,
                "wire_status": result.get("status"),
                "source_tool": result.get("source_tool"),
                "started_at": result.get("started_at"),
                "completed_at": result.get("completed_at"),
                "duration_ms": result.get("duration_ms"),
                "error_code": error.get("code"),
                "error_message": error.get("message"),
                "partial": bool(payload.get("partial")) if payload else False,
                "closed_volume": payload.get("closed_volume") if payload else None,
                "remaining_volume": payload.get("remaining_volume") if payload else None,
                "reconciled": payload.get("reconciled") if payload else None,
                # P3 v2 (§14): the negotiated command contract, the
                # server-lifecycle state, and whether this result is
                # a deduped replay answer rather than a fresh
                # execution — so the UI can show what actually
                # happened without a new execution surface.
                "command_protocol_version": result.get("command_protocol_version"),
                "deduplicated": bool(result.get("deduplicated")),
                "server_lifecycle": (result.get("server_lifecycle") or {}).get("current")
                if isinstance(result.get("server_lifecycle"), dict)
                else None,
                # S4 execution-hardening status (existing surface only):
                # attempts spent, broker retcode, and the idempotent /
                # reconciled outcomes, so the UI can show what actually
                # happened without a new execution surface.
                "retcode": payload.get("retcode") if payload else None,
                "retcode_label": payload.get("retcode_label") if payload else None,
                "attempts": payload.get("attempts") if payload else None,
                "already_closed": payload.get("already_closed") if payload else None,
                "no_changes": payload.get("no_changes") if payload else None,
                "history": [list(step) for step in execution.history],
            }
        )

    # ------------------------------------------------------------------
    def execute(self, frame: Any) -> tuple[CommandExecution, dict[str, Any]]:
        """Execute one command, then report its terminal state as one
        structured telemetry event (INFO for lifecycle, WARN for
        UNKNOWN/failed — INFO is never queued for delivery, §8)."""
        execution, result = self._execute_inner(frame)
        telemetry = self.telemetry
        if telemetry is not None:
            error = result.get("error") if isinstance(result, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else None
            state = execution.state
            if state is CommandState.COMPLETED:
                telemetry.info(
                    "command.completed",
                    component="server.commands",
                    command_id=execution.command_id,
                    details={"status": result.get("status")},
                )
            elif state is CommandState.PARTIALLY_COMPLETED:
                telemetry.info(
                    "command.partially_completed",
                    component="server.commands",
                    command_id=execution.command_id,
                    details={
                        "status": result.get("status"),
                        "partial": True,
                        "closed_volume": (result.get("payload") or {}).get("closed_volume")
                        if isinstance(result.get("payload"), dict)
                        else None,
                        "remaining_volume": (result.get("payload") or {}).get("remaining_volume")
                        if isinstance(result.get("payload"), dict)
                        else None,
                    },
                )
            elif state is CommandState.UNKNOWN:
                telemetry.warn(
                    "command.unknown",
                    component="server.commands",
                    command_id=execution.command_id,
                    error_code="TRADE_UNVERIFIED",
                    details={
                        "status": result.get("status"),
                        "recovery": "reconcile MT5 state before any retry; never blind-retry",
                    },
                )
            elif state is CommandState.EXPIRED:
                telemetry.info(
                    "command.expired",
                    component="server.commands",
                    command_id=execution.command_id,
                    error_code="Expired",
                    details={"status": result.get("status")},
                )
            elif state is CommandState.FAILED:
                telemetry.warn(
                    "command.failed",
                    component="server.commands",
                    command_id=execution.command_id,
                    error_code=error_code,
                    details={"status": result.get("status")},
                )
        self._record_recent(frame, execution, result)
        return execution, result

    def _idempotency_key(self, envelope: CommandEnvelope) -> str:
        return envelope.idempotency_key or envelope.command_id

    def _redeliver(self, stored: dict[str, Any], command_id: str) -> tuple[CommandState, dict[str, Any]]:
        """Re-issue a stored terminal result for a replayed command.

        A fresh ``message_id`` marks this as a new delivery of the
        same outcome; ``correlation_id`` is rewritten when the replay
        arrived under a new ``command_id`` sharing the idempotency
        key. The stored outcome itself is never re-computed — the
        point of the dedupe is that MT5 is not touched twice.
        """
        copy = dict(stored)
        copy["message_id"] = new_uuid7_str()
        copy["correlation_id"] = command_id
        copy["deduplicated"] = True
        state = CommandState.COMPLETED if copy.get("status") == "executed" else CommandState.FAILED
        return state, copy

    def _finish_rejection(
        self,
        execution: CommandExecution,
        envelope: CommandEnvelope,
        reason: RejectionReason,
        started: datetime,
        lifecycle: ServerLifecycle,
    ) -> tuple[CommandExecution, dict[str, Any]]:
        """Terminal rejection AFTER successful validation (dispatch
        re-checks: expiry, fencing). The result is stored by
        idempotency key — a replay of this command returns the same
        rejection instead of re-validating against a changed world.
        """
        result = rejection_result(
            device_id=self.validator.device_id,
            command_id=envelope.command_id,
            account_id=envelope.account_id,
            reason=reason,
            operation_type=envelope.operation_type,
            user_id=self.user_id,
            started_at=started,
        )
        result["command_protocol_version"] = envelope.protocol_version
        lifecycle.record("expired" if reason is RejectionReason.EXPIRED else "failed")
        result["server_lifecycle"] = lifecycle.to_dict()
        execution.transition(CommandState.EXPIRED if reason is RejectionReason.EXPIRED else CommandState.FAILED)
        execution.result = result
        self.idempotency_store.put(self._idempotency_key(envelope), result)
        self._persist(result)
        return execution, result

    def _execute_inner(self, frame: Any) -> tuple[CommandExecution, dict[str, Any]]:
        started = datetime.now(UTC)
        command_id = frame.get("command_id") if isinstance(frame, dict) else None
        command_id = str(command_id) if command_id else new_uuid7_str()
        execution = CommandExecution(command_id=command_id)
        lifecycle = ServerLifecycle(command_id=command_id)
        outcome = self.validator.validate(frame)
        if not outcome.ok:
            reason = outcome.rejection or RejectionReason.MALFORMED
            # P3 v2 §14.5 dedupe on replay: a redelivered command
            # whose terminal result is already stored is answered
            # with that result — never a second MT5 execution, and
            # never a bare "Replayed" for a command that already
            # completed. No stored result (validated but never
            # executed, e.g. a crash before dispatch) falls through
            # to the plain Replayed rejection.
            if reason is RejectionReason.REPLAYED and isinstance(frame, dict):
                key = str(frame.get("idempotency_key") or command_id)
                stored = self.idempotency_store.get(key)
                if stored is not None:
                    state, result = self._redeliver(stored, command_id)
                    lifecycle.record("confirmed" if state is CommandState.COMPLETED else "failed")
                    result["server_lifecycle"] = lifecycle.to_dict()
                    # Walk the legal chain: a replayed answer still
                    # passed validation once — the local lifecycle
                    # records the replay as a re-delivery, not a
                    # fresh execution.
                    execution.transition(CommandState.VALIDATED)
                    execution.transition(CommandState.AUTHORIZED)
                    if state is CommandState.COMPLETED:
                        execution.transition(CommandState.DISPATCHED)
                        execution.transition(CommandState.EXECUTING)
                    execution.transition(state)
                    execution.result = result
                    self._persist(result)
                    return execution, result
            account_id = ""
            operation_type = ""
            if isinstance(frame, dict):
                account_id = str(frame.get("account_id") or "")
                operation = frame.get("operation")
                if isinstance(operation, dict):
                    operation_type = str(operation.get("type") or "")
            result = rejection_result(
                device_id=self.validator.device_id,
                command_id=command_id,
                account_id=account_id,
                reason=reason,
                operation_type=operation_type,
                user_id=self.user_id,
                started_at=started,
            )
            lifecycle.record("expired" if reason is RejectionReason.EXPIRED else "failed")
            result["server_lifecycle"] = lifecycle.to_dict()
            execution.transition(CommandState.EXPIRED if reason is RejectionReason.EXPIRED else CommandState.FAILED)
            execution.result = result
            self._persist(result)
            return execution, result

        envelope = outcome.envelope
        assert envelope is not None
        execution.transition(CommandState.VALIDATED)
        lifecycle.record("delivered")
        execution.transition(CommandState.AUTHORIZED)
        # Idempotency pre-check: the same key under a NEW command_id
        # is the same submission — answer from the store, never
        # execute twice.
        stored = self.idempotency_store.get(self._idempotency_key(envelope))
        if stored is not None:
            state, result = self._redeliver(stored, envelope.command_id)
            lifecycle.record("confirmed" if state is CommandState.COMPLETED else "failed")
            result["server_lifecycle"] = lifecycle.to_dict()
            # Already AUTHORIZED at this point in the flow; walk the
            # remaining legal chain to the stored terminal state.
            if state is CommandState.COMPLETED:
                execution.transition(CommandState.DISPATCHED)
                execution.transition(CommandState.EXECUTING)
            execution.transition(state)
            execution.result = result
            self._persist(result)
            return execution, result
        # Re-check expiry at dispatch: a command that expired while
        # queued must be EXPIRED, never executed (§6.2/§6.6). Uses the
        # injected clock (same frozen clock the validator uses in
        # tests), not the wall clock, so validation and dispatch agree.
        if (normalize_utc(envelope.expires_at, 0) or started) <= self._clock():
            return self._finish_rejection(execution, envelope, RejectionReason.EXPIRED, started, lifecycle)

        execution.transition(CommandState.DISPATCHED)
        execution.transition(CommandState.EXECUTING)
        lifecycle.record("acknowledged")
        if envelope.operation_type == "open_order" and (envelope.order_kind or "market") != "market":
            source_tool = "trade_send_pending_order"
        else:
            source_tool = SOURCE_TOOL[envelope.operation_type]
        # §8.6 policy fencing: a command authorized under a
        # superseded revision never executes. Unconfigured (None) =
        # the server is the authority; the Bridge enforces the check
        # only when an expected revision is set.
        if (
            envelope.policy_revision is not None
            and self.expected_policy_revision is not None
            and envelope.policy_revision != self.expected_policy_revision
        ):
            logger.warning(
                "[COMMAND] {} fenced — policy_revision {} != expected {}",
                envelope.command_id,
                envelope.policy_revision,
                self.expected_policy_revision,
            )
            return self._finish_rejection(execution, envelope, RejectionReason.FENCED, started, lifecycle)
        # Per-device fencing: a single active command per (device,
        # fence token). The fence is always released — a stuck fence
        # would wedge the device's whole command path.
        fence_token = CommandFence.token_for(envelope)
        if not self.fence.acquire(self.validator.device_id, fence_token, envelope.command_id, self.fence_timeout_s):
            logger.warning(
                "[COMMAND] {} fenced — another command holds the fence for device {} token {}",
                envelope.command_id,
                self.validator.device_id,
                fence_token,
            )
            return self._finish_rejection(execution, envelope, RejectionReason.FENCED, started, lifecycle)
        try:
            dispatch_started = time.monotonic()
            try:
                if envelope.operation_type in ("modify_sl_tp", "modify_pending"):
                    with self._modify_lock:
                        payload = self._dispatch(envelope)
                else:
                    payload = self._dispatch(envelope)
            except BridgeError as exc:
                return self._finish_error(execution, envelope, source_tool, exc, started, lifecycle)
            except Exception:  # noqa: BLE001 — driver surprises are mcp_failed, never a crash
                logger.exception("[COMMAND] {} crashed during dispatch", envelope.operation_type)
                bridge_exc = BridgeError(ErrorCode.INTERNAL, "unexpected driver failure during dispatch")
                return self._finish_error(execution, envelope, source_tool, bridge_exc, started, lifecycle)
            dispatch_s = time.monotonic() - dispatch_started
            completed = datetime.now(UTC)
            validation_errors: list[str] = []
            if dispatch_s > self.execution_timeout_s:
                # Overrun: the dispatch took longer than the command
                # is worth waiting for. The returned payload is not
                # trusted on its own — reconcile against live MT5
                # truth first. A proven landing is reported executed;
                # an unprovable one is UNKNOWN, never a blind success.
                logger.warning(
                    "[COMMAND] {} dispatch overran execution_timeout_s ({:.1f}s > {:.1f}s) — reconciling",
                    envelope.command_id,
                    dispatch_s,
                    self.execution_timeout_s,
                )
                recovered = self._reconcile(envelope)
                if recovered is not None:
                    payload = recovered
                    validation_errors = ["execution_timeout_reconciled"]
                else:
                    lifecycle.record("unknown")
                    result = build_action_result(
                        device_id=self.validator.device_id,
                        command_id=envelope.command_id,
                        account_id=envelope.account_id,
                        status="mcp_failed",
                        source_tool=source_tool,
                        started_at=started,
                        completed_at=completed,
                        payload={},
                        user_id=self.user_id,
                        command_protocol_version=envelope.protocol_version,
                        server_lifecycle=lifecycle,
                        error=_error_object(
                            code=ErrorCode.TIMEOUT.value,
                            category="Execution",
                            source_action=source_tool,
                            cause=f"dispatch overran execution_timeout_s ({dispatch_s:.1f}s) and the outcome "
                            "could not be proven against MT5 truth",
                            recovery="Reconcile the actual MT5 state before any retry; never blind-retry a "
                            "non-idempotent op.",
                            retry_safe=False,
                            server_intervention_required=True,
                        ),
                    )
                    execution.transition(CommandState.UNKNOWN)
                    execution.result = result
                    self.idempotency_store.put(self._idempotency_key(envelope), result)
                    self._persist(result)
                    return execution, result
            # Partial execution is a distinct LOCAL terminal state (spec §7);
            # the wire status stays "executed" (§6.5 has no partial status —
            # dependency D5) with the partial facts in the payload.
            # Record "confirmed" BEFORE building the result: the
            # envelope snapshots the lifecycle at build time.
            lifecycle.record("confirmed")
            result = build_action_result(
                device_id=self.validator.device_id,
                command_id=envelope.command_id,
                account_id=envelope.account_id,
                status="executed",
                source_tool=source_tool,
                started_at=started,
                completed_at=completed,
                payload=payload,
                user_id=self.user_id,
                command_protocol_version=envelope.protocol_version,
                server_lifecycle=lifecycle,
                validation_errors=validation_errors or None,
            )
            if isinstance(payload, dict) and payload.get("partial") is True:
                execution.transition(CommandState.PARTIALLY_COMPLETED)
            else:
                execution.transition(CommandState.COMPLETED)
            execution.result = result
            self.idempotency_store.put(self._idempotency_key(envelope), result)
            self._persist(result)
            return execution, result
        finally:
            self.fence.release(self.validator.device_id, fence_token, envelope.command_id)

    # ------------------------------------------------------------------
    def _dispatch(self, envelope: CommandEnvelope) -> dict[str, Any]:
        from ..engine.orders import _fetch_order
        from ..engine.trading import (
            close_position,
            fetch_position,
            modify_pending_order_sltp,
            modify_position_sltp,
        )

        if envelope.operation_type == "close_position":
            # W3: an explicit volume (PROPOSED Phase 2b amendment) turns
            # the close into the reduce-volume operation — the local
            # partial-close path with its full reconciliation facts.
            # Absent volume keeps the v1 full-close semantics.
            return close_position(self.ctx, envelope.ticket_int, envelope.volume)
        if envelope.operation_type == "partial_close":
            # P3 v2 §14.2: a partial close MUST close strictly less
            # than the position volume — a volume equal to (or above)
            # it is a full close and belongs on close_position, never
            # silently promoted here.
            if envelope.volume is None or envelope.volume <= 0:
                raise BridgeError(
                    ErrorCode.VALIDATION,
                    "partial_close requires a positive volume.",
                    http_status=422,
                    details={"ticket": envelope.ticket_int},
                )
            pos = fetch_position(self.ctx, envelope.ticket_int)
            from ..mt5.types import f as _f

            pos_volume = float(_f(pos, "volume", 0.0) or 0.0)
            if envelope.volume >= pos_volume - 1e-9:
                raise BridgeError(
                    ErrorCode.VALIDATION,
                    f"Partial close volume {envelope.volume} must be smaller than position volume {pos_volume}. "
                    "Use close_position for a full close.",
                    http_status=422,
                    details={"ticket": envelope.ticket_int, "position_volume": pos_volume},
                )
            return close_position(self.ctx, envelope.ticket_int, envelope.volume)
        if envelope.operation_type == "cancel_order":
            # P3 v2 §14.2: cancel a pending order by ticket.
            from ..engine.orders import cancel_order_by_ticket

            return cancel_order_by_ticket(self.ctx, envelope.ticket_int)
        if envelope.operation_type == "modify_sl_tp":
            pos = fetch_position(self.ctx, envelope.ticket_int)
            from ..mt5.types import f as _f

            sl = envelope.sl if envelope.sl is not None else float(_f(pos, "sl", 0.0) or 0.0)
            tp = envelope.tp if envelope.tp is not None else float(_f(pos, "tp", 0.0) or 0.0)
            return modify_position_sltp(self.ctx, envelope.ticket_int, sl, tp)
        if envelope.operation_type == "modify_pending":
            order = _fetch_order(self.ctx, envelope.ticket_int)
            if order is None:
                raise BridgeError(
                    ErrorCode.TICKET_NOT_FOUND,
                    f"Pending order #{envelope.ticket} not found.",
                    details={"ticket": envelope.ticket_int},
                )
            from ..mt5.types import f as _f

            sl = envelope.sl if envelope.sl is not None else float(_f(order, "sl", 0.0) or 0.0)
            tp = envelope.tp if envelope.tp is not None else float(_f(order, "tp", 0.0) or 0.0)
            return modify_pending_order_sltp(self.ctx, envelope.ticket_int, sl, tp)
        if envelope.operation_type == "open_order":
            # W5 / §17.5: reachable ONLY through the activation gate
            # (``open_order_authorized=True``). The execution path runs
            # the proven local primitives — market (CONTRACTS §7 wire
            # shape) or pending (proposed shape in the PR body) — both
            # of which carry the mandatory pre-send ``order_check`` and
            # the W4 reconciliation semantics.
            return self._dispatch_open_order(envelope)
        raise BridgeError(ErrorCode.VALIDATION, f"unsupported operation {envelope.operation_type!r}")

    def _dispatch_open_order(self, envelope: CommandEnvelope) -> dict[str, Any]:
        """P3 v2 open path (market or pending) — via the v2 hook.

        The operation is re-parsed at the v2 seam
        (:func:`V2OpenOrderRequest.from_operation`), which enforces
        the §14.2 structural requirements against the ORIGINAL
        operation object — Platform wire names ``order_type`` /
        ``stop_limit`` included. A malformed open is a VALIDATION
        failure, never a guessed order; an unknown kind is a loud
        ``ValueError`` here, mapped onto VALIDATION below (the
        executor reports it ``mcp_failed``, never INTERNAL).
        """
        from ..engine.v2_open_order import V2OpenOrderRequest, execute_v2_open_order, negotiate_protocol_version

        operation = envelope.raw.get("operation")
        if not isinstance(operation, dict):
            raise BridgeError(ErrorCode.VALIDATION, "open_order operation is not a JSON object.", http_status=422)
        try:
            request = V2OpenOrderRequest.from_operation(operation)
        except (ValueError, TypeError) as exc:
            raise BridgeError(ErrorCode.VALIDATION, f"open_order is malformed: {exc}", http_status=422) from exc
        # The envelope already carries the parsed v2 fields; the
        # request built from the raw operation is authoritative for
        # the wire names (Bridge name wins on conflicts, per PR #20).
        # Deviation / expiry / comment travel on the request, not the
        # envelope — a mismatch between the two parsers is a bug, so
        # the seam above is the single place that reads them.
        return execute_v2_open_order(
            self.ctx,
            request,
            flag_enabled=self.validator.open_order_authorized,
            negotiation=negotiate_protocol_version(envelope.protocol_version),
        )

    def _finish_error(
        self,
        execution: CommandExecution,
        envelope: CommandEnvelope,
        source_tool: str,
        exc: BridgeError,
        started: datetime,
        lifecycle: ServerLifecycle,
    ) -> tuple[CommandExecution, dict[str, Any]]:
        """Map a driver failure onto FAILED or UNKNOWN (§6.5 + §7 of
        clarification #2).

        ``TRADE_UNVERIFIED`` means the outcome is uncertain: reconcile
        the actual MT5 state first. If reconciliation proves the op
        landed, the result is ``executed`` (recovered); otherwise the
        lifecycle is UNKNOWN and the result is ``mcp_failed`` with
        ``retry_safe: False`` — never a blind retry. Terminal results
        are stored by idempotency key (§14.5).
        """
        completed = datetime.now(UTC)
        if exc.code is ErrorCode.TRADE_UNVERIFIED:
            recovered = self._reconcile(envelope)
            if recovered is not None:
                lifecycle.record("confirmed")
                result = build_action_result(
                    device_id=self.validator.device_id,
                    command_id=envelope.command_id,
                    account_id=envelope.account_id,
                    status="executed",
                    source_tool=source_tool,
                    started_at=started,
                    completed_at=completed,
                    payload=recovered,
                    user_id=self.user_id,
                    command_protocol_version=envelope.protocol_version,
                    server_lifecycle=lifecycle,
                    validation_errors=["ambiguous_fill_recovered"],
                )
                execution.transition(CommandState.COMPLETED)
                execution.result = result
                self.idempotency_store.put(self._idempotency_key(envelope), result)
                self._persist(result)
                return execution, result
            lifecycle.record("unknown")
            result = build_action_result(
                device_id=self.validator.device_id,
                command_id=envelope.command_id,
                account_id=envelope.account_id,
                status="mcp_failed",
                source_tool=source_tool,
                started_at=started,
                completed_at=completed,
                payload={},
                user_id=self.user_id,
                command_protocol_version=envelope.protocol_version,
                server_lifecycle=lifecycle,
                error=_error_object(
                    code=exc.code.value,
                    category="Execution",
                    source_action=source_tool,
                    cause=exc.message,
                    recovery="Reconcile the actual MT5 state before any retry; never blind-retry a non-idempotent op.",
                    retry_safe=False,
                    server_intervention_required=True,
                ),
            )
            execution.transition(CommandState.UNKNOWN)
            execution.result = result
            self.idempotency_store.put(self._idempotency_key(envelope), result)
            self._persist(result)
            return execution, result
        category = "Prerequisite" if exc.code in (ErrorCode.SYMBOL_NOT_FOUND,) else "Execution"
        lifecycle.record("failed")
        result = build_action_result(
            device_id=self.validator.device_id,
            command_id=envelope.command_id,
            account_id=envelope.account_id,
            status="mcp_failed",
            source_tool=source_tool,
            started_at=started,
            completed_at=completed,
            payload={},
            user_id=self.user_id,
            command_protocol_version=envelope.protocol_version,
            server_lifecycle=lifecycle,
            error=_error_object(
                code=exc.code.value,
                category=category,
                source_action=source_tool,
                cause=exc.message,
                retry_safe=False,
            ),
        )
        execution.transition(CommandState.FAILED)
        execution.result = result
        self.idempotency_store.put(self._idempotency_key(envelope), result)
        self._persist(result)
        return execution, result

    def _sl_tp_tolerance(self, symbol: str) -> float:
        """Half a broker tick for SL/TP reconciliation (R6).

        Brokers normalize SL/TP to tick size, so a landed modification
        can differ from the requested value by up to half a tick;
        comparing with 1e-9 reported landed modifications as UNKNOWN.
        Falls back to 1e-9 when the symbol's tick size/point is unknown
        or the lookup fails — the reconcile must never crash on it.
        """
        try:
            from ..engine.common import require_symbol_info

            if not symbol:
                return 1e-9
            spec = require_symbol_info(self.ctx, symbol)
            step = spec.trade_tick_size or spec.point
            if step is None or float(step) <= 0:
                return 1e-9
            # +1e-9 absorbs binary-float noise at the boundary (the
            # decimal diff 1.085015 - 1.08501 is exactly half a tick,
            # but the floats differ by 5.000000000023874e-06); a full
            # tick is >= 1e-5, so this cannot false-recover an unlanded
            # modification.
            return float(step) / 2.0 + 1e-9
        except Exception:  # noqa: BLE001 — tolerance lookup is best-effort
            return 1e-9

    def _reconcile(self, envelope: CommandEnvelope) -> dict[str, Any] | None:
        """Check the actual MT5 state after an uncertain result.

        Returns a recovered payload when the op demonstrably landed,
        else None (UNKNOWN). Never retries the op itself.
        """
        from ..engine.orders import _fetch_order
        from ..engine.trading import fetch_position
        from ..mt5.types import f as _f

        try:
            if envelope.operation_type == "close_position":
                try:
                    fetch_position(self.ctx, envelope.ticket_int)
                except BridgeError as exc:
                    if exc.code is ErrorCode.TICKET_NOT_FOUND:
                        return {
                            "status": "done",
                            "ticket": envelope.ticket_int,
                            "ambiguous_fill_recovered": True,
                        }
                return None
            if envelope.operation_type == "partial_close":
                # A partial close is provable only from the live book
                # — and this reconcile does not know the pre-close
                # volume, so a still-open position proves nothing
                # about how much closed. Only a GONE position is
                # proof (the whole position closed); otherwise the
                # outcome stays UNKNOWN and the operator checks the
                # book. The submitted request is never evidence.
                try:
                    fetch_position(self.ctx, envelope.ticket_int)
                except BridgeError as exc:
                    if exc.code is ErrorCode.TICKET_NOT_FOUND:
                        return {
                            "status": "done",
                            "ticket": envelope.ticket_int,
                            "ambiguous_fill_recovered": True,
                            "remaining_volume": 0.0,
                        }
                    raise
                return None
            if envelope.operation_type == "cancel_order":
                # A cancel landed iff the pending order is gone from
                # the active book (it may sit in history as CANCELED).
                if _fetch_order(self.ctx, envelope.ticket_int) is None:
                    return {
                        "status": "done",
                        "ticket": envelope.ticket_int,
                        "ambiguous_fill_recovered": True,
                    }
                return None
            if envelope.operation_type == "open_order":
                # W5/W4: an uncertain open reconciles against live
                # positions with the same fingerprint the engine's
                # ambiguous-fill recovery uses (side + magic + recency).
                # The submitted request is never evidence of execution.
                from ..engine.trading import fetch_positions
                from ..mt5.types import f as _f

                want_type = 0 if envelope.side == "buy" else 1  # ORDER_TYPE_BUY / SELL
                cutoff = time.time() - 120.0
                for raw in fetch_positions(self.ctx, envelope.symbol or None):
                    if int(_f(raw, "type", -1)) != want_type:
                        continue
                    magic = self.ctx.settings.magic
                    if magic is not None and int(_f(raw, "magic", -1)) != int(magic):
                        continue
                    opened = float(_f(raw, "time", 0) or 0)
                    if opened and opened >= cutoff:
                        return {
                            "status": "done",
                            "ticket": int(_f(raw, "ticket", 0)),
                            "symbol": envelope.symbol,
                            "side": envelope.side,
                            "volume": float(_f(raw, "volume", 0.0) or 0.0),
                            "ambiguous_fill_recovered": True,
                        }
                return None
            if envelope.operation_type == "modify_sl_tp":
                pos = fetch_position(self.ctx, envelope.ticket_int)
                sl = float(_f(pos, "sl", 0.0) or 0.0)
                tp = float(_f(pos, "tp", 0.0) or 0.0)
                want_sl = envelope.sl if envelope.sl is not None else sl
                want_tp = envelope.tp if envelope.tp is not None else tp
                tolerance = self._sl_tp_tolerance(str(_f(pos, "symbol", "") or ""))
                if abs(sl - want_sl) <= tolerance and abs(tp - want_tp) <= tolerance:
                    return {
                        "status": "done",
                        "ticket": envelope.ticket_int,
                        "sl": sl,
                        "tp": tp,
                        "ambiguous_fill_recovered": True,
                    }
                return None
            if envelope.operation_type == "modify_pending":
                order = _fetch_order(self.ctx, envelope.ticket_int)
                if order is None:
                    return None
                sl = float(_f(order, "sl", 0.0) or 0.0)
                tp = float(_f(order, "tp", 0.0) or 0.0)
                want_sl = envelope.sl if envelope.sl is not None else sl
                want_tp = envelope.tp if envelope.tp is not None else tp
                tolerance = self._sl_tp_tolerance(str(_f(order, "symbol", "") or ""))
                if abs(sl - want_sl) <= tolerance and abs(tp - want_tp) <= tolerance:
                    return {
                        "status": "done",
                        "ticket": envelope.ticket_int,
                        "sl": sl,
                        "tp": tp,
                        "ambiguous_fill_recovered": True,
                    }
                return None
        except BridgeError:
            return None
        return None

    def _persist(self, result: dict[str, Any]) -> None:
        if self.results_queue is not None:
            self.results_queue.put(result, item_id=result.get("correlation_id"))

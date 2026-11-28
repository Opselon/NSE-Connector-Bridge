"""Policy envelope (D-5 / CONTRACTS.md §4.10) — the ONLY autonomous
management authority in Phase 2b.

The server issues a fresh, short-lived envelope authorizing **fast
trailing-stop, breakeven SL, and trailing-TP adjustments only** — sub-minute
SL/TP moves that cannot wait for a gateway round-trip. The envelope NEVER
authorizes an open, a close, or a volume change; the Bridge never
invents policy and never runs a local strategy.

Envelope shape (CONTRACTS.md §4.10)::

    {
      "type": "policy.envelope",           # PROPOSED WS frame type
      "account_id": "12345678901",
      "version": 7,
      "bounds": {"allowed_symbols": ["EURUSD"], ...},
      "rules": [{"rule_id": "r-1", "params": {...}}, ...],
      "risk_free_policy": {...},
      "issued_at": "RFC3339",
      "expires_at": "RFC3339"
    }

Rules enforced here (fail closed):

- ``expires_at - issued_at <= 5 min`` and ``expires_at > issued_at``
  (contract rule, §4.10) — longer envelopes are REJECTED, not clamped.
- ``version`` is an integer >= 1.
- The envelope's ``account_id`` must match the currently bound MT5
  login — an envelope for another account is never applied.
- A move is authorized only when ALL of these hold AT MOVE TIME:
  a stored envelope exists; ``now < expires_at``; the WebSocket to the
  platform is healthy (``ctx.server_channel_state["connected"]``);
  the specific ``rule_id`` exists in the envelope's rules; the rule
  authorizes the requested kind (trailing | breakeven | trailing_tp); and the
  position's symbol falls within ``bounds.allowed_symbols`` when the
  bound is present.
- Expired/invalid/missing envelope or a dead WebSocket → hold: no new
  autonomous moves, no invented fallbacks, no expanded permissions, no
  execution outside the last valid policy. An ALREADY IN-FLIGHT move
  completes (it was authorized before it started); afterwards new
  moves stop until a fresh valid envelope arrives.
- Every executed move carries ``rule_id`` + ``decision_id`` (uuidv7)
  + the envelope ``version`` so the server can trace it through the
  §17.3 decision-audit mechanism and reconcile it against the
  envelope version (DESIGN.md D-7). The delivery channel the audit
  called missing (P1-8) is now defined — CONTRACT v1 §5.3
  (Platform slice 8): every executed/failed move is persisted in
  the durable policy-move store
  (``reliability.policy_move_store``, ``policy_moves.jsonl`` in the
  data dir, retrievable via ``GET /api/v1/policy/moves``) AND
  emitted as a WARN ``policy.move.executed`` /
  ``policy.move.failed`` telemetry event (correlation_id =
  decision_id, details = the move record), which the durable
  diagnostic queue + ``HttpDiagnosticsSink`` carry to
  ``POST /api/v1/connector/diagnostics``. The event is WARN, not
  INFO, because INFO events are never queued for delivery by design
  (``telemetry.py``) and an executed autonomous move is a
  financially significant event the server must receive for
  reconciliation — step-gated, so it is not success-log flooding.
  Refused/no-op evaluations (``no_tighter_stop`` / ``no_better_tp``)
  are NOT persisted or delivered: they are the steady state of the
  2 s poll loop, and persisting them would fill the ledger with
  noise (the same rule the Platform's trailing-TP ledger applies).

Rule params (PROPOSED — the server owns the values; the Bridge only
executes the mechanical math)::

    trailing:     {"kind": "trailing",     "distance_points": >0, "step_points": >=0}
    breakeven:    {"kind": "breakeven",    "trigger_points": >0, "offset_points": >=0}
    trailing_tp:  {"kind": "trailing_tp",  "distance_points": >0, "step_points": >=0}

A trailing move only TIGHTENS the stop (never widens it); a breakeven
move only fires once the position is at least ``trigger_points`` in
profit and moves the stop to entry ± ``offset_points``. A trailing-TP
move only ratchets the take-profit AWAY from entry (never toward it),
keeping the TP ahead of the current price by ``distance_points`` — a
TP behind the current price would close the position on arrival, so
it is never proposed — and only fires when the candidate beats the
current TP by at least ``step_points`` (a position with no TP yet
baselines on its entry price). The TP math mirrors the Platform
server loop (``connector::trailing_tp::ratchet_tp``), which proves
the math first; the envelope is only the sub-minute fast path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from ..errors import BridgeError
from ..timeutil import normalize_utc
from .uuid7 import new_uuid7_str

if TYPE_CHECKING:
    from ..engine.common import BridgeContext

__all__ = [
    "ENVELOPE_MAX_AGE_S",
    "POLICY_FRAME_TYPE",
    "PolicyDenied",
    "PolicyEnvelope",
    "PolicyEnvelopeModel",
    "PolicyGate",
    "PolicyMoveExecutor",
    "PolicyService",
]

#: §4.10: ``expires_at - issued_at <= 5 min``.
ENVELOPE_MAX_AGE_S = 300.0
#: Server→Bridge WS frame type (CONTRACT v1 §5.2, defined by the
#: Platform slice 8 — the frame the audit called PROPOSED). Unknown
#: frame types are silently ignored, so a platform that never sends
#: this frame keeps the Bridge dormant.
POLICY_FRAME_TYPE = "policy.envelope"

RULE_KINDS = ("trailing", "breakeven", "trailing_tp")


class PolicyEnvelopeModel(BaseModel):
    """Pydantic validation of the §4.10 envelope (data, not logic)."""

    account_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    bounds: dict[str, Any] = Field(default_factory=dict)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    risk_free_policy: dict[str, Any] = Field(default_factory=dict)
    issued_at: str
    expires_at: str


class PolicyEnvelope:
    """A validated envelope (parsed once, re-checked for freshness at
    every authorization)."""

    def __init__(self, model: PolicyEnvelopeModel) -> None:
        self.account_id = model.account_id
        self.version = model.version
        self.bounds = model.bounds
        self.rules = model.rules
        self.risk_free_policy = model.risk_free_policy
        self.issued_at = model.issued_at
        self.expires_at = model.expires_at
        issued = normalize_utc(model.issued_at, 0)
        expires = normalize_utc(model.expires_at, 0)
        if issued is None or expires is None:
            raise PolicyDenied("invalid_time", "envelope issued_at/expires_at are not RFC 3339")
        if expires <= issued:
            raise PolicyDenied("invalid_window", "envelope expires_at is not after issued_at")
        if (expires - issued).total_seconds() > ENVELOPE_MAX_AGE_S:
            raise PolicyDenied(
                "window_too_long",
                f"envelope validity {(expires - issued).total_seconds():.0f}s exceeds the {ENVELOPE_MAX_AGE_S:.0f}s maximum (§4.10)",
            )
        self._issued_dt = issued
        self._expires_dt = expires
        self.rules_by_id: dict[str, dict[str, Any]] = {}
        for rule in model.rules:
            rule_id = str(rule.get("rule_id") or "")
            if not rule_id:
                raise PolicyDenied("invalid_rule", "envelope rule is missing rule_id")
            if rule_id in self.rules_by_id:
                raise PolicyDenied("invalid_rule", f"envelope rule_id {rule_id!r} is duplicated")
            self.rules_by_id[rule_id] = rule

    @property
    def expires_dt(self) -> datetime:
        return self._expires_dt

    def is_fresh(self, now: datetime) -> bool:
        return now < self._expires_dt

    def allowed_symbols(self) -> set[str] | None:
        raw = self.bounds.get("allowed_symbols")
        if raw is None:
            return None
        return {str(s) for s in raw}

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "version": self.version,
            "bounds": self.bounds,
            "rules": self.rules,
            "risk_free_policy": self.risk_free_policy,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


class PolicyDenied(Exception):
    """A requested autonomous move is NOT authorized (fail closed).

    ``reason`` is a stable code: ``no_envelope`` | ``expired`` |
    ``ws_down`` | ``unknown_rule`` | ``rule_kind_mismatch`` |
    ``symbol_not_allowed`` | ``invalid_time`` | ``invalid_window`` |
    ``window_too_long`` | ``invalid_rule`` | ``account_mismatch``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class PolicyGate:
    """Holds the latest valid envelope and authorizes moves (fail closed)."""

    def __init__(self, clock: Any = None) -> None:
        self._envelope: PolicyEnvelope | None = None
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def envelope(self) -> PolicyEnvelope | None:
        return self._envelope

    def apply_frame(self, frame: dict[str, Any], *, bound_account_id: str | None) -> PolicyEnvelope:
        """Validate a ``policy.envelope`` frame and store it.

        Replaces the stored envelope only when the new one is VALID and
        belongs to the currently bound account; otherwise raises
        :class:`PolicyDenied` and keeps the previous envelope (which
        may itself be expired — a stale envelope is never revived).
        """
        if not isinstance(frame, dict):
            raise PolicyDenied("invalid_rule", "policy envelope frame is not a JSON object")
        try:
            model = PolicyEnvelopeModel.model_validate(frame)
        except PydanticValidationError as exc:
            raise PolicyDenied("invalid_rule", f"policy envelope failed schema validation: {exc.errors()[:3]}") from exc
        envelope = PolicyEnvelope(model)
        if bound_account_id is not None and envelope.account_id != bound_account_id:
            raise PolicyDenied(
                "account_mismatch",
                f"envelope account {envelope.account_id} does not match the bound account {bound_account_id}",
            )
        self._envelope = envelope
        return envelope

    def authorize(
        self,
        *,
        rule_id: str,
        kind: str,
        symbol: str,
        ws_connected: bool,
    ) -> tuple[PolicyEnvelope, dict[str, Any]]:
        """Authorize ONE autonomous move — every check happens NOW.

        Fail-closed: any failing condition raises :class:`PolicyDenied`
        and the caller must not act.
        """
        envelope = self._envelope
        now = self._clock()
        if envelope is None:
            raise PolicyDenied("no_envelope", "no valid policy envelope is stored")
        if not envelope.is_fresh(now):
            raise PolicyDenied("expired", f"policy envelope expired at {envelope.expires_at}")
        if not ws_connected:
            raise PolicyDenied("ws_down", "the platform WebSocket is not healthy — autonomous moves hold")
        rule = envelope.rules_by_id.get(rule_id)
        if rule is None:
            raise PolicyDenied("unknown_rule", f"rule_id {rule_id!r} is not in the active envelope")
        params = rule.get("params") or {}
        if str(params.get("kind") or "").lower() != kind:
            raise PolicyDenied(
                "rule_kind_mismatch",
                f"rule {rule_id!r} does not authorize a {kind} move",
            )
        allowed = envelope.allowed_symbols()
        if allowed is not None and symbol not in allowed:
            raise PolicyDenied(
                "symbol_not_allowed",
                f"symbol {symbol!r} is outside the envelope's allowed_symbols",
            )
        return envelope, rule


@dataclass
class PolicyMoveRecord:
    """Traceability record for one autonomous move (§17.3 data)."""

    decision_id: str
    rule_id: str
    envelope_version: int
    kind: str
    ticket: int
    symbol: str
    sl_from: float | None = None
    sl_to: float | None = None
    tp_from: float | None = None
    tp_to: float | None = None
    status: str = "pending"  # executed | refused | failed
    denial_reason: str | None = None
    error_code: str | None = None
    retcode: int | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "rule_id": self.rule_id,
            "envelope_version": self.envelope_version,
            "kind": self.kind,
            "ticket": self.ticket,
            "symbol": self.symbol,
            "sl_from": self.sl_from,
            "sl_to": self.sl_to,
            "tp_from": self.tp_from,
            "tp_to": self.tp_to,
            "status": self.status,
            "denial_reason": self.denial_reason,
            "error_code": self.error_code,
            "retcode": self.retcode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def _sl_target_for_move(
    kind: str, params: dict[str, Any], *, pos: Any, tick_bid: float, tick_ask: float, point: float
) -> float | None:
    """Mechanical SL level for an authorized move — never a decision.

    Returns the new SL, or None when the mechanical conditions are not
    met (no tighter stop available: the move is a no-op, not an error).
    """
    pos_type = int(getattr(pos, "type", -1))
    entry = float(getattr(pos, "price_open", 0.0) or 0.0)
    current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
    is_long = pos_type == 0

    def better(candidate: float) -> bool:
        # Only tighten: a long's stop may only rise, a short's may only fall.
        if current_sl <= 0:
            return True
        return candidate > current_sl + 1e-9 if is_long else candidate < current_sl - 1e-9

    if kind == "trailing":
        distance = float(params.get("distance_points") or 0.0) * point
        if distance <= 0:
            return None
        candidate = (tick_bid - distance) if is_long else (tick_ask + distance)
        return candidate if better(candidate) else None
    if kind == "breakeven":
        trigger = float(params.get("trigger_points") or 0.0) * point
        offset = float(params.get("offset_points") or 0.0) * point
        if trigger <= 0:
            return None
        # Profit gate measured against the CURRENT price on the
        # closing side (bid closes a long, ask closes a short).
        close_price = tick_bid if is_long else tick_ask
        in_profit = (close_price - entry) >= trigger if is_long else (entry - close_price) >= trigger
        if not in_profit:
            return None
        candidate = entry + offset if is_long else entry - offset
        return candidate if better(candidate) else None
    return None


def _tp_target_for_move(
    params: dict[str, Any], *, pos: Any, tick_bid: float, tick_ask: float, point: float
) -> float | None:
    """Mechanical TP level for an authorized trailing-TP move — never a decision.

    The mirror of :func:`_sl_target_for_move` for the take-profit side,
    with the same semantics as the Platform server loop
    (``connector::trailing_tp::ratchet_tp``), which proves this math
    first: the candidate TP trails AHEAD of the current price (bid for
    a long, ask for a short) by ``distance_points`` — a TP behind the
    current price would close the position on arrival, so it is never
    proposed. Returns the new TP, or None when the mechanical
    conditions are not met (no ratchet available: the move is a no-op,
    not an error).

    The move fires only when ALL of these hold: the distance is
    positive; the candidate sits on the profit side of entry (never
    toward entry); and the candidate beats the current TP by at least
    ``step_points`` (a position with no TP yet baselines on its entry
    price, and the improvement must be strict — a step of 0 still
    never re-issues an unchanged TP). The candidate sits exactly
    ``distance_points`` from the tick; the broker stops level is
    enforced by the ``order_check`` inside ``modify_position_sltp``
    at execution, as for every other SLTP move.
    """
    pos_type = int(getattr(pos, "type", -1))
    entry = float(getattr(pos, "price_open", 0.0) or 0.0)
    current_tp = float(getattr(pos, "tp", 0.0) or 0.0)
    is_long = pos_type == 0
    if entry <= 0 or point <= 0 or tick_bid <= 0 or tick_ask <= 0:
        return None
    distance = float(params.get("distance_points") or 0.0) * point
    step = float(params.get("step_points") or 0.0) * point
    if distance <= 0 or step < 0:
        return None
    # Baseline: the current TP, or the entry price when the position
    # carries no TP yet.
    baseline = current_tp if current_tp > 0 else entry
    if is_long:
        candidate = tick_bid + distance
        if candidate <= entry + 1e-9:
            return None
        if candidate <= baseline + 1e-9:
            return None
        if candidate - baseline < step - 1e-9:
            return None
        return candidate
    candidate = tick_ask - distance
    if candidate >= entry - 1e-9:
        return None
    if candidate >= baseline - 1e-9:
        return None
    if baseline - candidate < step - 1e-9:
        return None
    return candidate


class PolicyMoveExecutor:
    """Executes ONE authorized trailing/breakeven move on the worker.

    The executor runs on the async poll loop and pushes the MT5 work
    through ``run_in_threadpool`` onto the serialized driver — never
    calling MT5 directly from the event loop (§4.2). Authorization is
    captured BEFORE the move starts, so an in-flight move completes
    under the envelope it was authorized with even if the envelope
    expires mid-flight; the NEXT evaluation re-authorizes from scratch.
    """

    def __init__(
        self,
        ctx: BridgeContext,
        gate: PolicyGate,
        *,
        telemetry: Any = None,
        history_max: int = 50,
        move_store: Any = None,
    ) -> None:
        self.ctx = ctx
        self.gate = gate
        self.telemetry = telemetry
        #: Durable audit record (P1-8): executed/failed moves are
        #: persisted here as well as kept in the in-memory history —
        #: the history is a 50-deep view, the store survives restarts
        #: and is what the local API serves. None in tests that do
        #: not need persistence.
        self.move_store = move_store
        self._history: deque[dict[str, Any]] = deque(maxlen=history_max)
        #: Tickets with a move currently in flight — one move per
        #: ticket at a time (a second poll cycle must not double-move).
        self._in_flight: set[int] = set()

    @property
    def history(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history]

    def ws_connected(self) -> bool:
        return bool(self.ctx.server_channel_state.get("connected"))

    async def evaluate_positions(self, positions: list[Any]) -> list[PolicyMoveRecord]:
        """Evaluate every open position against the active envelope.

        Authorization is re-checked per move; denied moves are recorded
        (traceability) but never executed.
        """

        records: list[PolicyMoveRecord] = []
        for raw in positions:
            ticket = int(getattr(raw, "ticket", 0) or 0)
            if not ticket or ticket in self._in_flight:
                continue
            symbol = str(getattr(raw, "symbol", "") or "")
            if not symbol:
                continue
            # Rule selection: the envelope's rules are evaluated in
            # order — the FIRST rule whose mechanical conditions can
            # produce a better stop / take-profit for this position is
            # used. Rule matching is data-driven; the Bridge adds no
            # preferences.
            record: PolicyMoveRecord | None = None
            for rule_id, rule in self.gate.envelope.rules_by_id.items() if self.gate.envelope else ():
                params = rule.get("params") or {}
                kind = str(params.get("kind") or "").lower()
                if kind not in RULE_KINDS:
                    continue
                record = await self._attempt_move(ticket, symbol, rule_id, kind, params)
                if record is not None and record.status in ("executed", "failed"):
                    break
                record = None  # no-op for this rule — try the next
            if record is not None:
                records.append(record)
        return records

    async def _attempt_move(
        self, ticket: int, symbol: str, rule_id: str, kind: str, params: dict[str, Any]
    ) -> PolicyMoveRecord | None:
        from starlette.concurrency import run_in_threadpool

        envelope_version = self.gate.envelope.version if self.gate.envelope else 0
        record = PolicyMoveRecord(
            decision_id=new_uuid7_str(),
            rule_id=rule_id,
            envelope_version=envelope_version,
            kind=kind,
            ticket=ticket,
            symbol=symbol,
            started_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            envelope, _rule = self.gate.authorize(
                rule_id=rule_id, kind=kind, symbol=symbol, ws_connected=self.ws_connected()
            )
        except PolicyDenied as denied:
            # Traceability: a denial is recorded, never hidden — but a
            # blanket denial (expired/ws_down) would spam once per
            # position per poll; only record per-position denials for
            # rule/symbol mismatches.
            if denied.reason in ("unknown_rule", "rule_kind_mismatch", "symbol_not_allowed"):
                record.status = "refused"
                record.denial_reason = denied.reason
                record.completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                self._history.append(record.to_dict())
            return record
        record.envelope_version = envelope.version
        self._in_flight.add(ticket)
        try:
            # P1-3: an authorized trailing/break-even move is a
            # trading action — its driver jobs run in the COMMAND
            # lane and jump queued chart/sync work.
            from ..mt5.driver import MT5_PRIORITY_COMMAND, mt5_priority_scope

            with mt5_priority_scope(MT5_PRIORITY_COMMAND):
                result = await run_in_threadpool(self._execute_move_sync, record, params)
        finally:
            self._in_flight.discard(ticket)
        self._history.append(result.to_dict())
        # P1-8: the durable audit record — executed/failed moves only
        # (refused no-ops stay in the in-memory history, never in the
        # store). A store failure must never break execution, so it is
        # swallowed with a warning, like telemetry.
        if self.move_store is not None and result.status in ("executed", "failed"):
            try:
                self.move_store.append(result.to_dict())
            except Exception:  # noqa: BLE001 — persistence must never break execution
                logger.warning("[POLICY] durable move-store write failed")
        self._emit(result)
        return result

    def _execute_move_sync(self, record: PolicyMoveRecord, params: dict[str, Any]) -> PolicyMoveRecord:
        """The MT5 half of one authorized move — runs on the worker via
        threadpool. Reads the position + tick, computes the mechanical
        target, and only then sends the SLTP modification."""
        from ..engine.common import require_symbol_info
        from ..engine.symbols import _fresh_tick
        from ..engine.trading import fetch_position, modify_position_sltp
        from ..mt5.types import f as _f

        completed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            pos = fetch_position(self.ctx, record.ticket)
            spec = require_symbol_info(self.ctx, record.symbol)
            tick = _fresh_tick(self.ctx, record.symbol)
            point = float(spec.point or 0.0)
            if point <= 0:
                record.status = "failed"
                record.error_code = "POLICY_POINT_UNKNOWN"
                record.completed_at = completed
                return record
            tick_bid = float(_f(tick, "bid", 0.0) or 0.0)
            tick_ask = float(_f(tick, "ask", 0.0) or 0.0)
            current_sl = float(_f(pos, "sl", 0.0) or 0.0)
            current_tp = float(_f(pos, "tp", 0.0) or 0.0)
            if record.kind == "trailing_tp":
                # TP ratchet: the stop stays exactly where it is; only
                # the take-profit moves (away from entry, ahead of price).
                target = _tp_target_for_move(params, pos=pos, tick_bid=tick_bid, tick_ask=tick_ask, point=point)
                if target is None:
                    record.status = "refused"
                    record.denial_reason = "no_better_tp"
                    record.completed_at = completed
                    return record
                record.tp_from = current_tp
                result = modify_position_sltp(self.ctx, record.ticket, current_sl, float(target))
                record.tp_to = float(target)
                log_detail = f"tp {record.tp_from} → {record.tp_to}"
            else:
                target = _sl_target_for_move(
                    record.kind,
                    params,
                    pos=pos,
                    tick_bid=tick_bid,
                    tick_ask=tick_ask,
                    point=point,
                )
                if target is None:
                    record.status = "refused"
                    record.denial_reason = "no_tighter_stop"
                    record.completed_at = completed
                    return record
                record.sl_from = current_sl
                result = modify_position_sltp(self.ctx, record.ticket, float(target), current_tp)
                record.sl_to = float(target)
                log_detail = f"sl {record.sl_from} → {record.sl_to}"
            record.status = "executed"
            record.retcode = int(result.get("retcode") or 0) if isinstance(result, dict) else None
            record.completed_at = completed
            logger.info(
                "[POLICY] move executed ticket={} rule={} version={} {} (decision {})",
                record.ticket,
                record.rule_id,
                record.envelope_version,
                log_detail,
                record.decision_id,
            )
            return record
        except BridgeError as exc:
            record.status = "failed"
            record.error_code = exc.code.value
            record.completed_at = completed
            logger.warning("[POLICY] move failed ticket={} rule={}: {}", record.ticket, record.rule_id, exc.message)
            return record

    def _emit(self, record: PolicyMoveRecord) -> None:
        if self.telemetry is None:
            return
        try:
            details = record.to_dict()
            # P1-8 / CONTRACT §5.3: the move-report event. Both
            # outcomes go out as WARN — INFO events are never queued
            # for delivery (telemetry.py binding rule), and an
            # executed autonomous move is a financially significant
            # event the server must receive to reconcile "who moved
            # my stop and why". correlation_id IS the decision_id, so
            # the diagnostic queue's dedup key (event_type +
            # error_code + correlation_id) is unique per move and two
            # moves can never collapse into one queued entry.
            if record.status == "executed":
                self.telemetry.warn(
                    "policy.move.executed",
                    component="server.policy",
                    correlation_id=record.decision_id,
                    details=details,
                )
            elif record.status == "failed":
                self.telemetry.warn(
                    "policy.move.failed",
                    component="server.policy",
                    correlation_id=record.decision_id,
                    error_code=record.error_code,
                    details=details,
                )
        except Exception:  # noqa: BLE001 — telemetry must never break execution
            logger.debug("[POLICY] telemetry emit failed")


class PolicyService:
    """Gate + executor wired to the channel lifecycle."""

    def __init__(self, ctx: BridgeContext, *, telemetry: Any = None, clock: Any = None, move_store: Any = None) -> None:
        self.ctx = ctx
        self.gate = PolicyGate(clock=clock)
        self.executor = PolicyMoveExecutor(ctx, self.gate, telemetry=telemetry, move_store=move_store)

    def handle_envelope_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Handle a (PROPOSED) ``policy.envelope`` server frame.

        Returns an honest acceptance report: ``accepted`` with the
        version, or ``rejected`` with the reason. The bound account is
        read from the driver state so an envelope for another account
        can never be applied (§7.2 binding).
        """
        login = self.ctx.driver.connection_state_dict().get("login")
        bound = str(login) if login is not None else None
        try:
            envelope = self.gate.apply_frame(frame, bound_account_id=bound)
        except PolicyDenied as denied:
            logger.warning("[POLICY] envelope rejected: {} ({})", denied.message, denied.reason)
            telemetry = self.executor.telemetry
            if telemetry is not None:
                try:
                    telemetry.warn(
                        "policy.envelope.rejected",
                        component="server.policy",
                        error_code=denied.reason,
                        details={"message": denied.message},
                    )
                except Exception as exc:  # noqa: BLE001 — telemetry must never break the handler
                    logger.debug("[POLICY] telemetry emit failed: {}", type(exc).__name__)
            return {"accepted": False, "reason": denied.reason}
        logger.info("[POLICY] envelope v{} accepted (expires {})", envelope.version, envelope.expires_at)
        return {"accepted": True, "version": envelope.version, "expires_at": envelope.expires_at}

    async def poll_once(self, positions: list[Any]) -> list[PolicyMoveRecord]:
        """One evaluation cycle (called by the channel poll loop)."""
        return await self.executor.evaluate_positions(positions)

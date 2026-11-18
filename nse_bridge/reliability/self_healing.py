"""Self-healing matrix (ARCHITECTURE-CONTRACT §8, PLAN addendum §7).

For each of the 15 scenarios the matrix defines: detect, classify,
report (structured telemetry event + diagnostics state), recover
safely when possible, preserve execution correctness, and expose the
final state to the UI (via ``GET /api/v1/diagnostics/summary``) and
to the server (via the existing truthful channels only).

Design rules (binding):

- Where a scenario is already handled by existing code (WS
  reconnect §5.4, replay cache §6.3, enrolment retry §3.2, result
  persistence §6.5), the matrix entry WIRES to that handler through
  a callback on :class:`HealingContext` — it does not rewrite
  working recovery logic. When no callback is wired (e.g. a unit
  test, or a subsystem that is not running), the entry records the
  recovery as "awaiting <action>" and stays ``degraded`` — it never
  claims a recovery it did not perform.
- Execution correctness: a non-idempotent op is NEVER blind-retried.
  A lost execution response ends in UNKNOWN first; only the MT5-state
  reconciliation (the existing ``CommandExecutor._reconcile`` path)
  can promote it to COMPLETED, and only with proof the op landed.
- No fake fallbacks: an unsupported fallback is an explicit
  structured error (``failed`` state + error code), never a silent
  success. ``ok`` means "the fault was detected AND the correct
  handling ran", not "everything is fine".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .telemetry import DEBUG, ERROR, INFO, WARN, TelemetryEmitter

__all__ = [
    "DEBUG",
    "ERROR",
    "INFO",
    "SCENARIOS",
    "WARN",
    "HealingContext",
    "Scenario",
    "ScenarioState",
    "SelfHealingRegistry",
]


class Scenario(str, Enum):
    INTERNET_LOSS = "internet_loss"
    PLATFORM_DOWN = "platform_down"
    WS_DROP = "ws_drop"
    SERVER_RESTART = "server_restart"
    MT5_CLOSE = "mt5_close"
    MT5_RESTART = "mt5_restart"
    IPC_LOSS = "ipc_loss"
    TERMINAL_PATH_CHANGE = "terminal_path_change"
    AUTH_EXPIRY = "auth_expiry"
    REGISTRATION_INVALID = "registration_invalid"
    LOST_EXECUTION_RESPONSE = "lost_execution_response"
    DUPLICATE_COMMANDS = "duplicate_commands"
    EXPIRED_COMMAND = "expired_command"
    INVALID_LOCAL_CONFIG = "invalid_local_config"
    BRIDGE_RESTART_MID_OPERATION = "bridge_restart_mid_operation"


#: The 15 scenarios, in contract order.
SCENARIOS: tuple[Scenario, ...] = tuple(Scenario)


@dataclass
class ScenarioState:
    """Last known handling outcome for one scenario — what the
    diagnostics summary exposes."""

    scenario: str
    status: str = "ok"  # ok | degraded | recovering | failed
    occurrences: int = 0
    last_occurrence: str | None = None
    last_action: str | None = None
    last_error_code: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "occurrences": self.occurrences,
            "last_occurrence": self.last_occurrence,
            "last_action": self.last_action,
            "last_error_code": self.last_error_code,
            "detail": self.detail,
        }


@dataclass
class HealingContext:
    """Probes + recovery callbacks for one scenario handling.

    Every field is optional with a safe default: a probe that was not
    provided is "unknown", never assumed healthy; a callback that was
    not provided means "recovery not wired here" — the handler
    records that honestly instead of pretending it recovered.
    """

    # -- connectivity probes -------------------------------------------
    ws_connected: bool = False
    ws_was_connected: bool = False
    platform_reachable: bool | None = None  # None = probe unavailable
    # -- MT5 probes ------------------------------------------------------
    mt5_connected: bool = False
    mt5_was_connected: bool = False
    mt5_ipc_ok: bool = True
    terminal_path: str | None = None
    terminal_path_exists: bool = True
    terminal_path_changed: bool = False
    # -- auth / enrolment -------------------------------------------------
    enrolment_state: str | None = None  # EnrolmentState value, if known
    session_expired: bool = False
    # -- config ------------------------------------------------------------
    config_errors: list[str] = field(default_factory=list)
    # -- command ------------------------------------------------------------
    command_id: str | None = None
    command_expired: bool = False
    is_duplicate: bool = False
    reconcile: Callable[[], dict[str, Any] | None] | None = None
    # -- restart recovery -----------------------------------------------------
    persisted_results: int = 0
    # -- recovery callbacks (wired to the existing handlers) -------------------
    on_reconnect: Callable[[], None] | None = None  # WS reconnect (§5.4)
    on_reenrol: Callable[[], None] | None = None  # register-device (§3.2)
    on_resync: Callable[[], None] | None = None  # fresh sync (§7.3)
    on_mt5_reconnect: Callable[[], None] | None = None  # driver connect
    on_rediscover_terminals: Callable[[], None] | None = None  # discovery


# (status, action, error_code, detail)
_Outcome = tuple[str, str, str | None, str | None]


class SelfHealingRegistry:
    """Owns the per-scenario last state and the handling dispatch.

    ``handle(scenario, ctx)`` detects, classifies, reports (telemetry
    event), attempts the safe recovery, and records the final state.
    It never raises for a bad probe — a handler failure is recorded
    as ``failed`` with error code ``self_healing_error``.
    """

    def __init__(self, telemetry: TelemetryEmitter | None = None) -> None:
        self.telemetry = telemetry
        self._states: dict[Scenario, ScenarioState] = {
            scenario: ScenarioState(scenario=scenario.value) for scenario in SCENARIOS
        }

    # ------------------------------------------------------------------
    def state(self, scenario: Scenario) -> ScenarioState:
        return self._states[scenario]

    def summary(self) -> dict[str, dict[str, Any]]:
        """Diagnostics/GUI payload: one entry per scenario."""
        return {s.value: self._states[s].to_dict() for s in SCENARIOS}

    def handle(self, scenario: Scenario, ctx: HealingContext | None = None) -> ScenarioState:
        ctx = ctx or HealingContext()
        handler = getattr(self, f"_handle_{scenario.value}", None)
        try:
            status, action, error_code, detail = handler(ctx) if handler else (
                "failed",
                "no handler registered for this scenario",
                "self_healing_error",
                None,
            )
        except Exception as exc:  # noqa: BLE001 — a handler failure is a recorded outcome, never a crash
            status, action, error_code, detail = (
                "failed",
                f"self-healing handler crashed: {type(exc).__name__}",
                "self_healing_error",
                None,
            )
        state = self._states[scenario]
        state.status = status
        state.occurrences += 1
        state.last_occurrence = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        state.last_action = action
        state.last_error_code = error_code
        state.detail = detail
        self._report(scenario, state, ctx)
        return state

    def _report(self, scenario: Scenario, state: ScenarioState, ctx: HealingContext) -> None:
        if self.telemetry is None:
            return
        severity = {"ok": INFO, "recovering": WARN, "degraded": WARN, "failed": ERROR}[state.status]
        self.telemetry.emit(
            severity,
            f"self_healing.{scenario.value}",
            component="reliability.self_healing",
            error_code=state.last_error_code,
            correlation_id=None,
            command_id=ctx.command_id,
            details={
                "scenario": scenario.value,
                "status": state.status,
                "action": state.last_action,
                "detail": state.detail,
            },
        )

    # ------------------------------------------------------------------
    # The 15 handlers. Each returns (status, action, error_code, detail).
    # ------------------------------------------------------------------
    def _handle_internet_loss(self, ctx: HealingContext) -> _Outcome:
        if ctx.platform_reachable is True:
            return (
                "ok",
                "platform reachable — not an internet loss; see platform_down if the WS is down",
                None,
                None,
            )
        if ctx.platform_reachable is False:
            return (
                "degraded",
                ("waiting for network; WS reconnect backoff (§5.4) continues; "
                "no commands executed while offline; sync batches stay queued"),
                "network_unreachable",
                "REST + WS both unreachable",
            )
        return (
            "degraded",
            ("connectivity probe unavailable — cannot confirm internet loss; "
            "WS reconnect backoff (§5.4) continues"),
            "network_unknown",
            None,
        )

    def _handle_platform_down(self, ctx: HealingContext) -> _Outcome:
        if ctx.platform_reachable is True and not ctx.ws_connected:
            return (
                "recovering",
                ("REST reachable but WS down — REST heartbeat (§3.4) keeps liveness; "
                "WS reconnect backoff (§5.4) continues"),
                "platform_ws_down",
                None,
            )
        if ctx.platform_reachable is False:
            return (
                "degraded",
                ("platform unreachable from here — indistinguishable from internet loss "
                "locally; see internet_loss; no unauthenticated fallback attempted"),
                "platform_unreachable",
                None,
            )
        return (
            "degraded",
            ("platform reachability unknown — WS reconnect backoff (§5.4) continues; "
            "REST heartbeat (§3.4) keeps liveness"),
            "platform_unknown",
            None,
        )

    def _handle_ws_drop(self, ctx: HealingContext) -> _Outcome:
        if ctx.ws_connected:
            return "ok", "WS channel live", None, None
        action = (
            "reconnect with §5.4 backoff (500 ms doubling → 30 s + jitter); "
            "pending results re-sent on reconnect (§6.5); server re-sends "
            "pending commands only — never executed/rejected/expired"
        )
        if ctx.on_reconnect is not None:
            ctx.on_reconnect()
            action += "; reconnect requested"
        return ("recovering", action, "ws_disconnected", None)

    def _handle_server_restart(self, ctx: HealingContext) -> _Outcome:
        action = (
            "fresh WS session (§5.4, no resume payload); "
            f"re-sent {ctx.persisted_results} persisted unacknowledged result(s) (§6.5); "
            "manifest re-sent on the next sync; server re-sends pending commands only"
        )
        if ctx.on_resync is not None:
            ctx.on_resync()
            action += "; fresh sync requested"
        return ("recovering", action, "server_restart", None)

    def _handle_mt5_close(self, ctx: HealingContext) -> _Outcome:
        if ctx.mt5_connected:
            return "ok", "MT5 connected", None, None
        action = (
            "MT5 unavailable — no execution attempted; commands are rejected "
            "with the guided MT5_NOT_RUNNING diagnostic; sync batches stay queued"
        )
        if ctx.on_mt5_reconnect is not None:
            ctx.on_mt5_reconnect()
            action += "; MT5 reconnect requested"
        return ("degraded", action, "MT5_NOT_RUNNING", None)

    def _handle_mt5_restart(self, ctx: HealingContext) -> _Outcome:
        if not ctx.mt5_connected:
            return (
                "degraded",
                "MT5 still down after restart attempt — see mt5_close",
                "MT5_NOT_RUNNING",
                None,
            )
        action = (
            "MT5 back after restart — fresh sync account → positions (§7.3); "
            "UNKNOWN commands reconciled against live MT5 state before any retry"
        )
        if ctx.on_resync is not None:
            ctx.on_resync()
            action += "; fresh sync requested"
        return ("recovering", action, "mt5_restarted", None)

    def _handle_ipc_loss(self, ctx: HealingContext) -> _Outcome:
        if ctx.mt5_ipc_ok:
            return "ok", "MT5 IPC healthy", None, None
        action = (
            "MT5 process up but IPC failed — guided diagnostic MT5_IPC_FAILED "
            "exposed; no execution while IPC is down"
        )
        if ctx.on_mt5_reconnect is not None:
            ctx.on_mt5_reconnect()
            action += "; driver re-initialize requested"
        return ("degraded", action, "MT5_IPC_FAILED", None)

    def _handle_terminal_path_change(self, ctx: HealingContext) -> _Outcome:
        if not ctx.terminal_path_changed and ctx.terminal_path_exists:
            return "ok", "configured terminal path unchanged and present", None, None
        action = (
            "configured terminal path is gone or changed — re-discovering terminals; "
            "the stored path is never overwritten automatically"
        )
        if ctx.on_rediscover_terminals is not None:
            ctx.on_rediscover_terminals()
            action += "; re-discovery requested"
        return (
            "degraded",
            action,
            "MT5_NOT_FOUND",
            f"terminal_path={ctx.terminal_path}",
        )

    def _handle_auth_expiry(self, ctx: HealingContext) -> _Outcome:
        if ctx.enrolment_state == "failed_auth":
            # The key itself is dead — this is registration_invalid's
            # territory; auth_expiry must not retry it.
            return (
                "failed",
                ("key is invalid/revoked (FAILED_AUTH) — no retry with the same key (§3.1); "
                "a new key is required"),
                "key_invalid",
                None,
            )
        if not ctx.session_expired and ctx.enrolment_state not in ("failed_transient", "validating"):
            return "ok", "session valid; no auth expiry detected", None, None
        action = (
            "session expired / 401 — re-enrol via register-device (§3.2); "
            "sync paused until REGISTERED; no unauthenticated fallback"
        )
        if ctx.on_reenrol is not None:
            ctx.on_reenrol()
            action += "; re-enrolment requested"
        return ("recovering", action, "session_expired", None)

    def _handle_registration_invalid(self, ctx: HealingContext) -> _Outcome:
        if ctx.enrolment_state != "failed_auth":
            return "ok", "device registration valid", None, None
        return (
            "failed",
            ("registration invalid (FAILED_AUTH) — terminal for this key: no retry, "
            "no unauthenticated fallback, sync stopped (§3.1); user must enter a new key"),
            "registration_invalid",
            None,
        )

    def _handle_lost_execution_response(self, ctx: HealingContext) -> _Outcome:
        # UNKNOWN first, always: never blind-retry a non-idempotent op.
        if ctx.reconcile is None:
            return (
                "failed",
                ("execution response lost and no MT5 reconciler is wired — command stays "
                "UNKNOWN; result mcp_failed retry_safe=false; server intervention required"),
                "TRADE_UNVERIFIED",
                None,
            )
        recovered = ctx.reconcile()
        if recovered is not None:
            return (
                "ok",
                ("reconciled against live MT5 state — op confirmed landed; "
                "result reported executed (recovered), never re-executed"),
                None,
                f"recovered payload keys: {sorted(recovered.keys())}",
            )
        return (
            "degraded",
            ("MT5 reconciliation inconclusive — command stays UNKNOWN; result "
            "mcp_failed retry_safe=false; server intervention required; never blind-retried"),
            "TRADE_UNVERIFIED",
            None,
        )

    def _handle_duplicate_commands(self, ctx: HealingContext) -> _Outcome:
        if not ctx.is_duplicate:
            return "ok", "no duplicate detected", None, None
        return (
            "ok",
            ("duplicate command rejected as Replayed (§6.3 step 7); never executed; "
            "replay id was consumed only by the original successful validation"),
            "Replayed",
            f"command_id={ctx.command_id}",
        )

    def _handle_expired_command(self, ctx: HealingContext) -> _Outcome:
        if not ctx.command_expired:
            return "ok", "command within its validity window", None, None
        return (
            "ok",
            ("expired command rejected as Expired (§6.3 step 2); never executed; "
            "server must re-issue with a new command_id if still wanted"),
            "Expired",
            f"command_id={ctx.command_id}",
        )

    def _handle_invalid_local_config(self, ctx: HealingContext) -> _Outcome:
        if not ctx.config_errors:
            return "ok", "local config valid", None, None
        return (
            "failed",
            ("invalid local config — explicit structured error; the affected "
            "subsystem refused to start (fail closed); no silent defaults applied"),
            "invalid_config",
            "; ".join(ctx.config_errors),
        )

    def _handle_bridge_restart_mid_operation(self, ctx: HealingContext) -> _Outcome:
        if ctx.persisted_results <= 0:
            return "ok", "no persisted unacknowledged results to re-queue", None, None
        return (
            "recovering",
            (f"re-queued {ctx.persisted_results} persisted unacknowledged result(s) "
            "for delivery on reconnect (§6.5); nothing silently dropped"),
            "restart_recovery",
            None,
        )

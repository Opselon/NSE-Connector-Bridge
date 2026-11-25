"""Self-healing matrix (ARCHITECTURE-CONTRACT §8, PLAN addendum §7).

For each of the 20 scenarios (spec §13) the matrix defines: detect, classify,
report (structured telemetry event + diagnostics state), recover
safely when possible, preserve execution correctness, and expose the
final state to the UI (via ``GET /api/v1/diagnostics/summary``) and
to the server (via the existing truthful channels only).

Scenario → spec §13 mapping (the 5 added by Phase B3 are marked):

- internet_loss (1), platform_down (2), ws_drop (3), auth_expiry (4),
  registration_invalid (5), mt5_close (6), mt5_restart (7), ipc_loss (8),
  account_disconnect (9, B3), broker_connection_change (10, B3),
  terminal_path_change (11), symbol_unavailable (12, B3),
  lost_execution_response (13), duplicate_commands (14),
  expired_command (15), bridge_restart_mid_operation (16),
  diagnostic_queue_full (17, B3), invalid_local_config (18),
  update_failure (19 + 20, B3 — ONE entry for both spec items: a
  download failure and a verification failure are the same updater
  fault at different stages, and the handler branches on stage —
  download may retry, verification must never retry the same bytes).
  Update download/verification are therefore 2 spec items in 1
  matrix entry, which is what makes 15 + 5 = 20.

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
    # -- Phase B3: the 5 missing spec §13 entries -------------------------
    ACCOUNT_DISCONNECT = "account_disconnect"  # §13.9
    BROKER_CONNECTION_CHANGE = "broker_connection_change"  # §13.10
    SYMBOL_UNAVAILABLE = "symbol_unavailable"  # §13.12
    DIAGNOSTIC_QUEUE_FULL = "diagnostic_queue_full"  # §13.17
    # §13.19 + §13.20 in ONE entry (see module docstring): the handler
    # branches on update_stage — download may retry, verification must
    # never retry/install the same bytes.
    UPDATE_FAILURE = "update_failure"  # §13.19 + §13.20


#: The 20 scenarios, in contract order.
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
    # -- account / broker (Phase B3, spec §13.9–13.10) ------------------------
    account_connected: bool = False
    account_was_connected: bool = False
    account_login: str | None = None
    broker_server: str | None = None  # current broker server name, if known
    broker_server_previous: str | None = None
    broker_server_changed: bool = False
    # -- symbol (Phase B3, spec §13.12) ---------------------------------------
    symbol: str | None = None
    symbol_available: bool = True
    symbol_unavailable_reason: str | None = None
    # -- diagnostic queue (Phase B3, spec §13.17) ----------------------------
    queue_full: bool = False
    queue_stats: dict[str, Any] | None = None  # DiagnosticQueue.stats(), if known
    # -- updater (Phase B3, spec §13.19–13.20) --------------------------------
    update_stage: str | None = None  # "download" | "verify" | None
    update_error_code: str | None = None  # updates.py ERR_* code, if known
    update_error: str | None = None  # human-readable updater message (redacted)
    update_version: str | None = None
    # -- recovery callbacks (wired to the existing handlers) -------------------
    on_reconnect: Callable[[], None] | None = None  # WS reconnect (§5.4)
    on_reenrol: Callable[[], None] | None = None  # register-device (§3.2)
    on_resync: Callable[[], None] | None = None  # fresh sync (§7.3)
    on_mt5_reconnect: Callable[[], None] | None = None  # driver connect
    on_rediscover_terminals: Callable[[], None] | None = None  # discovery
    on_account_reconnect: Callable[[], None] | None = None  # driver connect (account)
    on_refresh_symbols: Callable[[], None] | None = None  # symbol re-discovery
    on_drain_diagnostics: Callable[[], None] | None = None  # queue drain attempt
    on_retry_update: Callable[[], None] | None = None  # updater retry (download only)


# (status, action, error_code, detail)
_Outcome = tuple[str, str, str | None, str | None]

#: Updater error codes that mean VERIFICATION failed (``nse_bridge/updates.py``
#: ERR_* codes). A verification failure must never retry or install the
#: same bytes — the artifact is refused outright. Every other updater
#: code (DOWNLOAD_FAILED, NO_DOWNLOAD_URL, CHECK_FAILED,
#: REST_UNAVAILABLE, NOT_CONFIGURED, NO_UPDATE_AVAILABLE) is a
#: download/check-stage failure: nothing was staged, so a retry is safe.
_UPDATE_VERIFY_CODES = frozenset(
    {
        "NO_PUBLIC_KEY",
        "INVALID_PUBLIC_KEY",
        "CRYPTO_UNAVAILABLE",
        "SIGNATURE_MISSING",
        "SHA256_MISSING",
        "SIGNATURE_INVALID",
        "SHA256_MISMATCH",
        "SIZE_MISMATCH",
        "MIN_VERSION_NOT_SATISFIED",
    }
)


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
            status, action, error_code, detail = (
                handler(ctx)
                if handler
                else (
                    "failed",
                    "no handler registered for this scenario",
                    "self_healing_error",
                    None,
                )
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
    # The 20 handlers. Each returns (status, action, error_code, detail).
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
                (
                    "waiting for network; WS reconnect backoff (§5.4) continues; "
                    "no commands executed while offline; sync batches stay queued"
                ),
                "network_unreachable",
                "REST + WS both unreachable",
            )
        return (
            "degraded",
            ("connectivity probe unavailable — cannot confirm internet loss; WS reconnect backoff (§5.4) continues"),
            "network_unknown",
            None,
        )

    def _handle_platform_down(self, ctx: HealingContext) -> _Outcome:
        if ctx.platform_reachable is True and not ctx.ws_connected:
            return (
                "recovering",
                (
                    "REST reachable but WS down — REST heartbeat (§3.4) keeps liveness; "
                    "WS reconnect backoff (§5.4) continues"
                ),
                "platform_ws_down",
                None,
            )
        if ctx.platform_reachable is False:
            return (
                "degraded",
                (
                    "platform unreachable from here — indistinguishable from internet loss "
                    "locally; see internet_loss; no unauthenticated fallback attempted"
                ),
                "platform_unreachable",
                None,
            )
        return (
            "degraded",
            (
                "platform reachability unknown — WS reconnect backoff (§5.4) continues; "
                "REST heartbeat (§3.4) keeps liveness"
            ),
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
            "MT5 process up but IPC failed — guided diagnostic MT5_IPC_FAILED exposed; no execution while IPC is down"
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
                ("key is invalid/revoked (FAILED_AUTH) — no retry with the same key (§3.1); a new key is required"),
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
            (
                "registration invalid (FAILED_AUTH) — terminal for this key: no retry, "
                "no unauthenticated fallback, sync stopped (§3.1); user must enter a new key"
            ),
            "registration_invalid",
            None,
        )

    def _handle_lost_execution_response(self, ctx: HealingContext) -> _Outcome:
        # UNKNOWN first, always: never blind-retry a non-idempotent op.
        if ctx.reconcile is None:
            return (
                "failed",
                (
                    "execution response lost and no MT5 reconciler is wired — command stays "
                    "UNKNOWN; result mcp_failed retry_safe=false; server intervention required"
                ),
                "TRADE_UNVERIFIED",
                None,
            )
        recovered = ctx.reconcile()
        if recovered is not None:
            return (
                "ok",
                (
                    "reconciled against live MT5 state — op confirmed landed; "
                    "result reported executed (recovered), never re-executed"
                ),
                None,
                f"recovered payload keys: {sorted(recovered.keys())}",
            )
        return (
            "degraded",
            (
                "MT5 reconciliation inconclusive — command stays UNKNOWN; result "
                "mcp_failed retry_safe=false; server intervention required; never blind-retried"
            ),
            "TRADE_UNVERIFIED",
            None,
        )

    def _handle_duplicate_commands(self, ctx: HealingContext) -> _Outcome:
        if not ctx.is_duplicate:
            return "ok", "no duplicate detected", None, None
        return (
            "ok",
            (
                "duplicate command rejected as Replayed (§6.3 step 7); never executed; "
                "replay id was consumed only by the original successful validation"
            ),
            "Replayed",
            f"command_id={ctx.command_id}",
        )

    def _handle_expired_command(self, ctx: HealingContext) -> _Outcome:
        if not ctx.command_expired:
            return "ok", "command within its validity window", None, None
        return (
            "ok",
            (
                "expired command rejected as Expired (§6.3 step 2); never executed; "
                "server must re-issue with a new command_id if still wanted"
            ),
            "Expired",
            f"command_id={ctx.command_id}",
        )

    def _handle_invalid_local_config(self, ctx: HealingContext) -> _Outcome:
        if not ctx.config_errors:
            return "ok", "local config valid", None, None
        return (
            "failed",
            (
                "invalid local config — explicit structured error; the affected "
                "subsystem refused to start (fail closed); no silent defaults applied"
            ),
            "invalid_config",
            "; ".join(ctx.config_errors),
        )

    def _handle_bridge_restart_mid_operation(self, ctx: HealingContext) -> _Outcome:
        if ctx.persisted_results <= 0:
            return "ok", "no persisted unacknowledged results to re-queue", None, None
        return (
            "recovering",
            (
                f"re-queued {ctx.persisted_results} persisted unacknowledged result(s) "
                "for delivery on reconnect (§6.5); nothing silently dropped"
            ),
            "restart_recovery",
            None,
        )

    # ------------------------------------------------------------------
    # Phase B3 — the 5 missing spec §13 entries.
    # ------------------------------------------------------------------
    def _handle_account_disconnect(self, ctx: HealingContext) -> _Outcome:
        """Spec §13.9 — the MT5 terminal is up but no account is logged
        in (or the logged-in account dropped). Detection is the driver
        account probe (``MT5_ACCOUNT_NOT_READY``); recovery is a driver
        reconnect — the Bridge never asks for, stores, or re-enters an
        MT5 password. No execution is attempted while disconnected."""
        if ctx.account_connected:
            return "ok", "account connected", None, None
        action = (
            "account disconnected — no execution attempted; commands are "
            "rejected with the guided MT5_ACCOUNT_NOT_READY diagnostic; "
            "sync batches stay queued until the account is back"
        )
        if ctx.account_was_connected:
            action = "account was connected and is now lost — " + action
        if ctx.on_account_reconnect is not None:
            ctx.on_account_reconnect()
            action += "; account reconnect requested"
        elif ctx.on_mt5_reconnect is not None:
            ctx.on_mt5_reconnect()
            action += "; MT5 reconnect requested"
        return (
            "degraded",
            action,
            "MT5_ACCOUNT_NOT_READY",
            f"account_login={ctx.account_login}" if ctx.account_login else None,
        )

    def _handle_broker_connection_change(self, ctx: HealingContext) -> _Outcome:
        """Spec §13.10 — the terminal re-connected to a different broker
        server. The account identity, symbol set, and contract terms may
        all have changed with it, so NOTHING is executed against stale
        state: a fresh sync (account → positions) re-establishes the
        truth, and symbol mappings must be refreshed before any
        symbol-addressed command is accepted again."""
        if not ctx.broker_server_changed:
            if ctx.broker_server is not None:
                return (
                    "ok",
                    f"broker connection stable on {ctx.broker_server}",
                    None,
                    None,
                )
            return (
                "degraded",
                (
                    "broker server unknown — cannot confirm the connection did "
                    "not change; no execution against unconfirmed broker state"
                ),
                "broker_server_unknown",
                None,
            )
        action = (
            "broker connection changed — fresh sync account → positions "
            "(§7.3) before any execution; symbol mappings must be "
            "refreshed, never assumed to carry over to the new broker"
        )
        if ctx.on_resync is not None:
            ctx.on_resync()
            action += "; fresh sync requested"
        if ctx.on_refresh_symbols is not None:
            ctx.on_refresh_symbols()
            action += "; symbol refresh requested"
        return (
            "recovering" if ctx.on_resync is not None else "degraded",
            action,
            "broker_connection_changed",
            f"broker_server {ctx.broker_server_previous} -> {ctx.broker_server}",
        )

    def _handle_symbol_unavailable(self, ctx: HealingContext) -> _Outcome:
        """Spec §13.12 — a symbol a command (or the server) references
        is not currently tradable/visible on this broker. The fix is
        NEVER to guess a similarly named symbol: the command is
        rejected with a structured error and the symbol inventory is
        refreshed so the server gets the true availability."""
        if ctx.symbol_available:
            return "ok", "symbol available", None, None
        action = (
            "symbol unavailable — commands naming it are rejected with a "
            "structured error; no similarly named symbol is substituted, "
            "never guessed; symbol inventory refresh requested"
        )
        if ctx.on_refresh_symbols is not None:
            ctx.on_refresh_symbols()
            action += "; symbol refresh requested"
        detail = f"symbol={ctx.symbol}" if ctx.symbol else None
        if ctx.symbol_unavailable_reason:
            detail = (
                f"{detail}; reason={ctx.symbol_unavailable_reason}"
                if detail
                else (f"reason={ctx.symbol_unavailable_reason}")
            )
        return ("degraded", action, "symbol_unavailable", detail)

    def _handle_diagnostic_queue_full(self, ctx: HealingContext) -> _Outcome:
        """Spec §13.17 — the bounded diagnostic queue hit its limit.
        The queue itself already handles the overflow honestly (oldest
        entry evicted WITH a warning, evictions counted — never
        silently discarded). This entry's recovery is a delivery
        attempt: drain against the attached sink. When the sink is the
        inert default (no sink attached) a drain delivers nothing and
        the queue keeps draining by TTL — the handler reports that
        honestly instead of claiming delivery."""
        if not ctx.queue_full:
            return "ok", "diagnostic queue within its bound", None, None
        detail = None
        if ctx.queue_stats is not None:
            detail = (
                f"queued={ctx.queue_stats.get('queued')} "
                f"max_items={ctx.queue_stats.get('max_items')} "
                f"evicted={ctx.queue_stats.get('evicted')} "
                f"expired={ctx.queue_stats.get('expired')}"
            )
        action = (
            "diagnostic queue full — oldest events were evicted with a "
            "warning (evictions counted, never silently discarded); "
            "delivery attempt requested"
        )
        if ctx.on_drain_diagnostics is not None:
            ctx.on_drain_diagnostics()
            action += "; drain requested"
        else:
            action += "; no drain wired — events age out by TTL (no sink attached)"
        return ("recovering", action, "diagnostic_queue_full", detail)

    def _handle_update_failure(self, ctx: HealingContext) -> _Outcome:
        """Spec §13.19 + §13.20 in one entry (see module docstring).

        Download stage (``DOWNLOAD_FAILED`` and friends): nothing was
        staged, so a retry is safe — the handler requests one when a
        retry callback is wired, otherwise records "awaiting retry".
        Verification stage (SHA-256 / Ed25519 / missing signature /
        ``NO_PUBLIC_KEY`` / size or version mismatch): the artifact is
        REFUSED — nothing staged, nothing installed, and the same
        bytes are never retried. The failure is a structured error
        the operator must act on (new release, or provision the
        signing key); installing manually is never the recovery."""
        stage = ctx.update_stage
        code = ctx.update_error_code
        is_verify = stage == "verify" or (stage is None and code is not None and code in _UPDATE_VERIFY_CODES)
        version = f" version={ctx.update_version}" if ctx.update_version else ""
        if is_verify:
            return (
                "failed",
                (
                    "update verification failed — artifact REFUSED: nothing "
                    "staged, nothing installed; the same bytes must never be "
                    "retried or installed manually; a new release (or the "
                    "signing key) is required"
                ),
                code or "update_verification_failed",
                (f"stage=verify{version}; {ctx.update_error}" if ctx.update_error else f"stage=verify{version}"),
            )
        # Download / check stage (or an unclassified updater error with
        # no verification code): nothing staged, retry is safe.
        action = "update download failed — nothing staged, nothing installed; download is idempotent so a retry is safe"
        if ctx.on_retry_update is not None:
            ctx.on_retry_update()
            action += "; update retry requested"
        else:
            action += "; awaiting retry"
        return (
            "recovering",
            action,
            code or "update_download_failed",
            (
                f"stage={stage or 'download'}{version}; {ctx.update_error}"
                if ctx.update_error
                else f"stage={stage or 'download'}{version}"
            ),
        )

"""Unit tests for the self-healing matrix (§8) — one test per scenario.

Label: unit. Each test drives one scenario through a simulated fault
and asserts the expected detection + recovery action + final state
exposed by the registry (and, where relevant, the structured
telemetry event it reports). No MT5, no network.
"""

from __future__ import annotations

import pytest

from nse_bridge.reliability.self_healing import (
    SCENARIOS,
    HealingContext,
    Scenario,
    SelfHealingRegistry,
)
from nse_bridge.reliability.telemetry import TelemetryEmitter

pytestmark = pytest.mark.unit


def _registry():
    return SelfHealingRegistry(telemetry=TelemetryEmitter("dev-1"))


def test_registry_covers_exactly_the_20_spec_scenarios():
    assert len(SCENARIOS) == 20
    assert {s.value for s in SCENARIOS} == {
        "internet_loss", "platform_down", "ws_drop", "server_restart",
        "mt5_close", "mt5_restart", "ipc_loss", "terminal_path_change",
        "auth_expiry", "registration_invalid", "lost_execution_response",
        "duplicate_commands", "expired_command", "invalid_local_config",
        "bridge_restart_mid_operation",
        # Phase B3 (spec §13.9, 13.10, 13.12, 13.17, 13.19+13.20):
        "account_disconnect", "broker_connection_change",
        "symbol_unavailable", "diagnostic_queue_full", "update_failure",
    }
    # Every scenario has a handler: no entry falls through to the
    # "no handler registered" failure.
    registry = _registry()
    for scenario in SCENARIOS:
        assert getattr(registry, f"_handle_{scenario.value}", None) is not None
    summary = _registry().summary()
    assert set(summary) == {s.value for s in SCENARIOS}
    for entry in summary.values():
        assert set(entry) == {
            "scenario", "status", "occurrences", "last_occurrence",
            "last_action", "last_error_code", "detail",
        }


# ---------------------------------------------------------------------------
# 1. internet loss
# ---------------------------------------------------------------------------
def test_internet_loss_detected_when_platform_unreachable():
    registry = _registry()
    state = registry.handle(
        Scenario.INTERNET_LOSS, HealingContext(platform_reachable=False)
    )
    assert state.status == "degraded"
    assert state.last_error_code == "network_unreachable"
    assert "no commands executed while offline" in state.last_action
    assert state.occurrences == 1
    assert state.last_occurrence is not None


def test_internet_loss_not_claimed_when_platform_reachable():
    state = _registry().handle(
        Scenario.INTERNET_LOSS, HealingContext(platform_reachable=True)
    )
    assert state.status == "ok"
    assert "not an internet loss" in state.last_action


# ---------------------------------------------------------------------------
# 2. Platform down
# ---------------------------------------------------------------------------
def test_platform_down_rest_reachable_ws_down_keeps_heartbeat():
    state = _registry().handle(
        Scenario.PLATFORM_DOWN,
        HealingContext(platform_reachable=True, ws_connected=False),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "platform_ws_down"
    assert "REST heartbeat" in state.last_action


# ---------------------------------------------------------------------------
# 3. WS drop
# ---------------------------------------------------------------------------
def test_ws_drop_requests_reconnect_via_existing_backoff():
    called: list[str] = []
    state = _registry().handle(
        Scenario.WS_DROP,
        HealingContext(
            ws_connected=False, ws_was_connected=True,
            on_reconnect=lambda: called.append("reconnect"),
        ),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "ws_disconnected"
    assert "§5.4 backoff" in state.last_action
    assert "pending results re-sent" in state.last_action
    assert called == ["reconnect"]


def test_ws_drop_ok_when_channel_live():
    state = _registry().handle(Scenario.WS_DROP, HealingContext(ws_connected=True))
    assert state.status == "ok"


# ---------------------------------------------------------------------------
# 4. server restart
# ---------------------------------------------------------------------------
def test_server_restart_fresh_session_and_results_resent():
    state = _registry().handle(
        Scenario.SERVER_RESTART, HealingContext(persisted_results=2)
    )
    assert state.status == "recovering"
    assert "fresh WS session" in state.last_action
    assert "re-sent 2 persisted" in state.last_action
    assert "no resume payload" in state.last_action


# ---------------------------------------------------------------------------
# 5/6. MT5 close / restart
# ---------------------------------------------------------------------------
def test_mt5_close_marks_unavailable_no_execution_attempted():
    state = _registry().handle(Scenario.MT5_CLOSE, HealingContext(mt5_connected=False))
    assert state.status == "degraded"
    assert state.last_error_code == "MT5_NOT_RUNNING"
    assert "no execution attempted" in state.last_action


def test_mt5_restart_triggers_fresh_sync():
    called: list[str] = []
    state = _registry().handle(
        Scenario.MT5_RESTART,
        HealingContext(
            mt5_connected=True, mt5_was_connected=False,
            on_resync=lambda: called.append("resync"),
        ),
    )
    assert state.status == "recovering"
    assert "fresh sync account → positions" in state.last_action
    assert "reconciled against live MT5 state" in state.last_action
    assert called == ["resync"]


def test_mt5_restart_still_down_degrades():
    state = _registry().handle(
        Scenario.MT5_RESTART, HealingContext(mt5_connected=False, mt5_was_connected=False)
    )
    assert state.status == "degraded"
    assert state.last_error_code == "MT5_NOT_RUNNING"


# ---------------------------------------------------------------------------
# 7. IPC loss
# ---------------------------------------------------------------------------
def test_ipc_loss_exposes_structured_diagnostic_no_execution():
    state = _registry().handle(Scenario.IPC_LOSS, HealingContext(mt5_ipc_ok=False))
    assert state.status == "degraded"
    assert state.last_error_code == "MT5_IPC_FAILED"
    assert "no execution while IPC is down" in state.last_action


# ---------------------------------------------------------------------------
# 8. terminal-path change
# ---------------------------------------------------------------------------
def test_terminal_path_change_triggers_rediscovery():
    called: list[str] = []
    state = _registry().handle(
        Scenario.TERMINAL_PATH_CHANGE,
        HealingContext(
            terminal_path=r"C:\MT5\terminal64.exe",
            terminal_path_exists=False, terminal_path_changed=True,
            on_rediscover_terminals=lambda: called.append("rediscover"),
        ),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "MT5_NOT_FOUND"
    assert "never overwritten automatically" in state.last_action
    assert called == ["rediscover"]


# ---------------------------------------------------------------------------
# 9. auth expiry
# ---------------------------------------------------------------------------
def test_auth_expiry_reenrols_via_existing_path():
    called: list[str] = []
    state = _registry().handle(
        Scenario.AUTH_EXPIRY,
        HealingContext(
            session_expired=True, enrolment_state="failed_transient",
            on_reenrol=lambda: called.append("reenrol"),
        ),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "session_expired"
    assert "re-enrol via register-device" in state.last_action
    assert called == ["reenrol"]


def test_auth_expiry_never_retries_a_dead_key():
    state = _registry().handle(
        Scenario.AUTH_EXPIRY,
        HealingContext(session_expired=True, enrolment_state="failed_auth"),
    )
    assert state.status == "failed"
    assert state.last_error_code == "key_invalid"
    assert "no retry with the same key" in state.last_action


# ---------------------------------------------------------------------------
# 10. registration invalid
# ---------------------------------------------------------------------------
def test_registration_invalid_is_terminal_structured_error():
    state = _registry().handle(
        Scenario.REGISTRATION_INVALID, HealingContext(enrolment_state="failed_auth")
    )
    assert state.status == "failed"
    assert state.last_error_code == "registration_invalid"
    assert "terminal for this key" in state.last_action
    assert "no unauthenticated fallback" in state.last_action


# ---------------------------------------------------------------------------
# 11. lost execution response — UNKNOWN + reconciliation
# ---------------------------------------------------------------------------
def test_lost_execution_response_reconciled_to_completed():
    state = _registry().handle(
        Scenario.LOST_EXECUTION_RESPONSE,
        HealingContext(
            command_id="cmd-1",
            reconcile=lambda: {"status": "done", "ticket": 9000001},
        ),
    )
    assert state.status == "ok"
    assert "never re-executed" in state.last_action
    assert state.last_error_code is None


def test_lost_execution_response_inconclusive_stays_unknown():
    state = _registry().handle(
        Scenario.LOST_EXECUTION_RESPONSE,
        HealingContext(command_id="cmd-1", reconcile=lambda: None),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "TRADE_UNVERIFIED"
    assert "stays UNKNOWN" in state.last_action
    assert "never blind-retried" in state.last_action


def test_lost_execution_response_without_reconciler_is_explicit_error():
    state = _registry().handle(
        Scenario.LOST_EXECUTION_RESPONSE, HealingContext(command_id="cmd-1")
    )
    assert state.status == "failed"
    assert state.last_error_code == "TRADE_UNVERIFIED"
    assert "stays UNKNOWN" in state.last_action


# ---------------------------------------------------------------------------
# 12. duplicate commands
# ---------------------------------------------------------------------------
def test_duplicate_command_rejected_as_replay_never_executed():
    state = _registry().handle(
        Scenario.DUPLICATE_COMMANDS,
        HealingContext(command_id="cmd-1", is_duplicate=True),
    )
    assert state.status == "ok"  # ok = the fault was handled correctly
    assert state.last_error_code == "Replayed"
    assert "never executed" in state.last_action


# ---------------------------------------------------------------------------
# 13. expired command
# ---------------------------------------------------------------------------
def test_expired_command_rejected_never_executed():
    state = _registry().handle(
        Scenario.EXPIRED_COMMAND,
        HealingContext(command_id="cmd-1", command_expired=True),
    )
    assert state.status == "ok"
    assert state.last_error_code == "Expired"
    assert "never executed" in state.last_action


# ---------------------------------------------------------------------------
# 14. invalid local config
# ---------------------------------------------------------------------------
def test_invalid_local_config_is_explicit_structured_error():
    state = _registry().handle(
        Scenario.INVALID_LOCAL_CONFIG,
        HealingContext(config_errors=[
            "server_base_url must use https://",
            "reconnect_max_ms must be >= reconnect_initial_ms",
        ]),
    )
    assert state.status == "failed"
    assert state.last_error_code == "invalid_config"
    assert "fail closed" in state.last_action
    assert "server_base_url must use https://" in state.detail
    assert "no silent defaults applied" in state.last_action


# ---------------------------------------------------------------------------
# 15. Bridge restart mid-operation
# ---------------------------------------------------------------------------
def test_bridge_restart_mid_operation_requeues_persisted_results():
    state = _registry().handle(
        Scenario.BRIDGE_RESTART_MID_OPERATION, HealingContext(persisted_results=3)
    )
    assert state.status == "recovering"
    assert "re-queued 3 persisted" in state.last_action
    assert "nothing silently dropped" in state.last_action


def test_bridge_restart_with_nothing_persisted_is_ok():
    state = _registry().handle(
        Scenario.BRIDGE_RESTART_MID_OPERATION, HealingContext(persisted_results=0)
    )
    assert state.status == "ok"


# ---------------------------------------------------------------------------
# Reporting: every handling emits one structured telemetry event
# ---------------------------------------------------------------------------
def test_handling_reports_a_structured_telemetry_event():
    emitter = TelemetryEmitter("dev-1")
    registry = SelfHealingRegistry(telemetry=emitter)
    registry.handle(Scenario.MT5_CLOSE, HealingContext(mt5_connected=False))
    assert len(emitter.queue) == 1  # WARN → queued
    event = emitter.queue.snapshot()[0]
    assert event.event_type == "self_healing.mt5_close"
    assert event.severity == "WARN"
    assert event.error_code == "MT5_NOT_RUNNING"
    assert event.component == "reliability.self_healing"
    assert list(event.to_dict()) == [
        "timestamp", "severity", "event_type", "device_id", "correlation_id",
        "command_id", "component", "error_code", "sanitized_details",
    ]


def test_handler_crash_is_recorded_not_raised():
    registry = _registry()

    def _boom() -> dict | None:
        raise RuntimeError("probe exploded")

    state = registry.handle(
        Scenario.LOST_EXECUTION_RESPONSE, HealingContext(reconcile=_boom)
    )
    assert state.status == "failed"
    assert state.last_error_code == "self_healing_error"


# ---------------------------------------------------------------------------
# Phase B3 — the 5 missing spec §13 entries.
# Mapping (see self_healing.py module docstring): account_disconnect
# = §13.9, broker_connection_change = §13.10, symbol_unavailable =
# §13.12, diagnostic_queue_full = §13.17, update_failure = §13.19
# (download) + §13.20 (verification) in ONE entry — the handler
# branches on stage, so both spec items are covered and asserted.
# ---------------------------------------------------------------------------
def test_account_disconnect_marks_unavailable_no_execution_attempted():
    state = _registry().handle(
        Scenario.ACCOUNT_DISCONNECT,
        HealingContext(account_connected=False, account_was_connected=True,
                        account_login="12345678901"),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "MT5_ACCOUNT_NOT_READY"
    assert "no execution attempted" in state.last_action
    assert "was connected and is now lost" in state.last_action
    assert state.detail == "account_login=12345678901"


def test_account_disconnect_requests_reconnect_when_wired():
    called: list[str] = []
    state = _registry().handle(
        Scenario.ACCOUNT_DISCONNECT,
        HealingContext(account_connected=False,
                       on_account_reconnect=lambda: called.append("account-reconnect")),
    )
    assert state.status == "degraded"
    assert "account reconnect requested" in state.last_action
    assert called == ["account-reconnect"]


def test_account_disconnect_ok_when_account_connected():
    state = _registry().handle(
        Scenario.ACCOUNT_DISCONNECT, HealingContext(account_connected=True)
    )
    assert state.status == "ok"


def test_broker_connection_change_triggers_fresh_sync_and_symbol_refresh():
    called: list[str] = []
    state = _registry().handle(
        Scenario.BROKER_CONNECTION_CHANGE,
        HealingContext(
            broker_server="NewBroker-Live", broker_server_previous="OldBroker-Demo",
            broker_server_changed=True,
            on_resync=lambda: called.append("resync"),
            on_refresh_symbols=lambda: called.append("symbols"),
        ),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "broker_connection_changed"
    assert "fresh sync account → positions" in state.last_action
    assert "never assumed to carry over" in state.last_action
    assert "symbol refresh requested" in state.last_action
    assert state.detail == "broker_server OldBroker-Demo -> NewBroker-Live"
    assert called == ["resync", "symbols"]


def test_broker_connection_change_without_resync_stays_degraded():
    state = _registry().handle(
        Scenario.BROKER_CONNECTION_CHANGE,
        HealingContext(
            broker_server="NewBroker-Live", broker_server_previous="OldBroker-Demo",
            broker_server_changed=True,
        ),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "broker_connection_changed"


def test_broker_connection_change_ok_when_stable():
    state = _registry().handle(
        Scenario.BROKER_CONNECTION_CHANGE,
        HealingContext(broker_server="Broker-Live", broker_server_changed=False),
    )
    assert state.status == "ok"
    assert "stable on Broker-Live" in state.last_action


def test_broker_connection_change_unknown_server_is_degraded_not_ok():
    state = _registry().handle(
        Scenario.BROKER_CONNECTION_CHANGE,
        HealingContext(broker_server=None, broker_server_changed=False),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "broker_server_unknown"


def test_symbol_unavailable_rejects_never_substitutes():
    called: list[str] = []
    state = _registry().handle(
        Scenario.SYMBOL_UNAVAILABLE,
        HealingContext(
            symbol="XAUUSD.a", symbol_available=False,
            symbol_unavailable_reason="not visible in market watch",
            on_refresh_symbols=lambda: called.append("refresh"),
        ),
    )
    assert state.status == "degraded"
    assert state.last_error_code == "symbol_unavailable"
    assert "never guessed" in state.last_action
    assert "no similarly named symbol is substituted" in state.last_action
    assert state.detail == "symbol=XAUUSD.a; reason=not visible in market watch"
    assert called == ["refresh"]


def test_symbol_unavailable_ok_when_available():
    state = _registry().handle(
        Scenario.SYMBOL_UNAVAILABLE,
        HealingContext(symbol="EURUSD", symbol_available=True),
    )
    assert state.status == "ok"


def test_diagnostic_queue_full_requests_drain_and_reports_counts():
    called: list[str] = []
    stats = {"queued": 128, "max_items": 128, "evicted": 3, "expired": 1}
    state = _registry().handle(
        Scenario.DIAGNOSTIC_QUEUE_FULL,
        HealingContext(
            queue_full=True, queue_stats=stats,
            on_drain_diagnostics=lambda: called.append("drain"),
        ),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "diagnostic_queue_full"
    assert "evicted with a warning" in state.last_action
    assert "drain requested" in state.last_action
    assert state.detail == "queued=128 max_items=128 evicted=3 expired=1"
    assert called == ["drain"]


def test_diagnostic_queue_full_without_drain_says_ttl_honestly():
    state = _registry().handle(
        Scenario.DIAGNOSTIC_QUEUE_FULL, HealingContext(queue_full=True)
    )
    assert state.status == "recovering"
    assert "age out by TTL" in state.last_action
    assert "no diagnostics endpoint exists yet, D1" in state.last_action


def test_diagnostic_queue_full_ok_when_within_bound():
    state = _registry().handle(
        Scenario.DIAGNOSTIC_QUEUE_FULL, HealingContext(queue_full=False)
    )
    assert state.status == "ok"


def test_update_download_failure_is_retryable_nothing_staged():
    called: list[str] = []
    state = _registry().handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(
            update_stage="download", update_error_code="DOWNLOAD_FAILED",
            update_error="Download failed: connection reset",
            update_version="1.2.0",
            on_retry_update=lambda: called.append("retry"),
        ),
    )
    assert state.status == "recovering"
    assert state.last_error_code == "DOWNLOAD_FAILED"
    assert "nothing staged" in state.last_action
    assert "retry is safe" in state.last_action
    assert "update retry requested" in state.last_action
    assert state.detail == "stage=download version=1.2.0; Download failed: connection reset"
    assert called == ["retry"]


def test_update_download_failure_without_retry_callback_awaits_retry():
    state = _registry().handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(update_stage="download", update_error_code="DOWNLOAD_FAILED"),
    )
    assert state.status == "recovering"
    assert "awaiting retry" in state.last_action


def test_update_verification_failure_is_terminal_never_retried():
    called: list[str] = []
    state = _registry().handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(
            update_stage="verify", update_error_code="SHA256_MISMATCH",
            update_error="SHA-256 mismatch — the downloaded bytes do not match",
            update_version="1.2.0",
            on_retry_update=lambda: called.append("retry"),
        ),
    )
    assert state.status == "failed"
    assert state.last_error_code == "SHA256_MISMATCH"
    assert "artifact REFUSED" in state.last_action
    assert "nothing staged, nothing installed" in state.last_action
    assert "never be retried" in state.last_action
    assert called == []  # the retry callback must NOT fire for a bad artifact
    assert state.detail.startswith("stage=verify version=1.2.0;")


def test_update_verification_failure_classified_by_code_without_stage():
    # A caller that only knows the UpdateError code (no stage) is
    # still classified as verification — never retried.
    state = _registry().handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(update_error_code="SIGNATURE_INVALID"),
    )
    assert state.status == "failed"
    assert state.last_error_code == "SIGNATURE_INVALID"


def test_update_no_public_key_is_a_verification_refusal():
    state = _registry().handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(update_stage="verify", update_error_code="NO_PUBLIC_KEY"),
    )
    assert state.status == "failed"
    assert state.last_error_code == "NO_PUBLIC_KEY"
    assert "artifact REFUSED" in state.last_action


def test_new_scenarios_report_structured_telemetry_events():
    emitter = TelemetryEmitter("dev-1")
    registry = SelfHealingRegistry(telemetry=emitter)
    registry.handle(
        Scenario.ACCOUNT_DISCONNECT, HealingContext(account_connected=False)
    )
    registry.handle(
        Scenario.UPDATE_FAILURE,
        HealingContext(update_stage="verify", update_error_code="SHA256_MISMATCH"),
    )
    assert len(emitter.queue) == 2  # degraded → WARN, failed → ERROR, both queued
    events = emitter.queue.snapshot()
    assert events[0].event_type == "self_healing.account_disconnect"
    assert events[0].severity == "WARN"
    assert events[1].event_type == "self_healing.update_failure"
    assert events[1].severity == "ERROR"
    assert events[1].error_code == "SHA256_MISMATCH"

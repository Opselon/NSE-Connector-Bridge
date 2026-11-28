"""Unit tests for the MT5 worker priority lane (audit P1-3).

Label: unit. No MT5 — the driver's worker thread runs against a
fake ``_module``; the fence and executor tests never touch a terminal.

Covers:
- the serialized worker still runs one job at a time (the driver is
  NOT parallelized — the lane only reorders QUEUED jobs);
- a command-lane job jumps background-lane jobs still queued, FIFO
  within a lane, URGENT (close) jumps COMMAND;
- the ambient lane (``mt5_priority_scope``) is what ``run`` reads
  when no explicit priority is passed, and an explicit priority wins;
- the queue-full fast failure and the priority validation survive;
- ``run_order_send`` runs in the COMMAND lane;
- per-ticket fence tokens: independent tickets run concurrently,
  the same ticket still serializes (fail-closed ``Fenced``).
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import (
    MT5_JOB_QUEUE_MAX,
    MT5_PRIORITY_BACKGROUND,
    MT5_PRIORITY_COMMAND,
    MT5_PRIORITY_NORMAL,
    MT5_PRIORITY_URGENT,
    Mt5Driver,
    current_mt5_priority,
    mt5_priority_scope,
)
from nse_bridge.server.commands import (
    CommandExecutor,
    CommandFence,
    CommandState,
    CommandValidator,
    RejectionReason,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DEVICE = "dev-1"
ACCOUNT = "12345678901"


def _driver(tmp_path: Path) -> Mt5Driver:
    settings = BridgeSettings(data_dir=tmp_path)
    driver = Mt5Driver(settings)
    # The worker thread runs jobs against whatever _module() returns;
    # a bare object is enough — test jobs ignore the mt5 argument.
    driver._module = lambda: object()  # type: ignore[method-assign]
    return driver


def _frame(ticket: str, **overrides):
    issued = NOW - timedelta(minutes=1)
    command_id = new_uuid7_str()
    frame = {
        "type": "command",
        "command_id": command_id,
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "close_position", "ticket": ticket},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": command_id,
    }
    frame.update(overrides)
    return frame


def _executor(fence_timeout_s: float = 60.0) -> CommandExecutor:
    settings = BridgeSettings(_env_file=None, server_token="<redacted>", device_id=DEVICE)
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    validator = CommandValidator(
        DEVICE,
        get_account_id=lambda: ACCOUNT,
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: NOW,
    )
    return CommandExecutor(ctx, validator, clock=lambda: NOW, fence_timeout_s=fence_timeout_s)


def _run_in_thread(driver: Mt5Driver, job, **kwargs) -> threading.Thread:
    """Run one driver.run call on a thread; returns the thread."""
    t = threading.Thread(target=lambda: driver.run(job, **kwargs), daemon=True)
    t.start()
    return t


# ----------------------------------------------------------------------
# Worker queue: lanes, FIFO, seriality
# ----------------------------------------------------------------------
def test_command_job_jumps_queued_background_jobs(tmp_path):
    driver = _driver(tmp_path)
    order: list[str] = []
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0), "gate job never started on the worker"
    bg = _run_in_thread(driver, lambda _m: order.append("background"), op="bg", priority=MT5_PRIORITY_BACKGROUND)
    cmd = _run_in_thread(driver, lambda _m: order.append("command"), op="cmd", priority=MT5_PRIORITY_COMMAND)
    time.sleep(0.2)  # let both jobs queue behind the gate job
    depths = driver.queue_depths()
    assert depths["background"] == 1 and depths["command"] == 1 and depths["total"] == 2
    gate.set()
    gate_thread.join(timeout=5.0)
    bg.join(timeout=5.0)
    cmd.join(timeout=5.0)
    assert order == ["command", "background"], "a command must jump queued background work"


def test_fifo_within_one_lane(tmp_path):
    driver = _driver(tmp_path)
    order: list[str] = []
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0)
    first = _run_in_thread(driver, lambda _m: order.append("first"), op="c1", priority=MT5_PRIORITY_COMMAND)
    second = _run_in_thread(driver, lambda _m: order.append("second"), op="c2", priority=MT5_PRIORITY_COMMAND)
    time.sleep(0.2)
    gate.set()
    gate_thread.join(timeout=5.0)
    first.join(timeout=5.0)
    second.join(timeout=5.0)
    assert order == ["first", "second"], "jobs in one lane must stay FIFO"


def test_urgent_close_jumps_command_lane(tmp_path):
    driver = _driver(tmp_path)
    order: list[str] = []
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0)
    normal_cmd = _run_in_thread(driver, lambda _m: order.append("command"), op="cmd", priority=MT5_PRIORITY_COMMAND)
    urgent = _run_in_thread(driver, lambda _m: order.append("urgent"), op="close", priority=MT5_PRIORITY_URGENT)
    time.sleep(0.2)
    gate.set()
    gate_thread.join(timeout=5.0)
    normal_cmd.join(timeout=5.0)
    urgent.join(timeout=5.0)
    assert order == ["urgent", "command"], "a close (URGENT) must jump other queued commands"


def test_worker_stays_serial(tmp_path):
    """The lane reorders queued jobs — it must never run two at once."""
    driver = _driver(tmp_path)
    lock = threading.Lock()
    active = {"n": 0, "max": 0}

    def job(_mt5):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.02)
        with lock:
            active["n"] -= 1

    threads = [_run_in_thread(driver, job, op=f"job-{i}", priority=MT5_PRIORITY_NORMAL) for i in range(12)]
    for t in threads:
        t.join(timeout=10.0)
    assert active["max"] == 1, "the MT5 worker must stay strictly serial"


def test_ambient_scope_sets_lane_and_explicit_priority_wins(tmp_path):
    driver = _driver(tmp_path)
    assert current_mt5_priority() == MT5_PRIORITY_NORMAL
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0)
    done: list[str] = []

    def run_with_copied_context(job, **kwargs) -> threading.Thread:
        # Mirror run_in_threadpool: the caller's context (including
        # the ambient lane) is copied into the worker thread.
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(driver.run, job, **kwargs), daemon=True)
        t.start()
        return t

    with mt5_priority_scope(MT5_PRIORITY_BACKGROUND):
        assert current_mt5_priority() == MT5_PRIORITY_BACKGROUND
        scoped = run_with_copied_context(lambda _m: done.append("scoped"), op="scoped")
        explicit = run_with_copied_context(
            lambda _m: done.append("explicit"), op="explicit", priority=MT5_PRIORITY_COMMAND
        )
    assert current_mt5_priority() == MT5_PRIORITY_NORMAL, "the scope must not leak"
    time.sleep(0.2)
    depths = driver.queue_depths()
    assert depths["background"] == 1, "the scoped job must queue in the background lane"
    assert depths["command"] == 1, "an explicit priority must win over the ambient scope"
    gate.set()
    gate_thread.join(timeout=5.0)
    scoped.join(timeout=5.0)
    explicit.join(timeout=5.0)
    assert done == ["explicit", "scoped"]


def test_queue_depths_return_to_zero_after_drain(tmp_path):
    """Lane depths are bookkeeping, not a leak: once every queued
    job has run, all lanes (and the total) must read 0 again."""
    driver = _driver(tmp_path)
    threads = [
        _run_in_thread(driver, lambda _m: None, op=f"job-{i}", priority=MT5_PRIORITY_BACKGROUND) for i in range(20)
    ]
    for t in threads:
        t.join(timeout=10.0)
    assert driver.queue_depths() == {"urgent": 0, "command": 0, "normal": 0, "background": 0, "total": 0}


def test_invalid_priority_is_rejected(tmp_path):
    driver = _driver(tmp_path)
    with pytest.raises(ValueError):
        driver.run(lambda _m: None, priority=10)
    with pytest.raises(ValueError):
        driver.run(lambda _m: None, priority=-1)
    with pytest.raises(ValueError), mt5_priority_scope(99):
        pass


def test_queue_full_still_fails_fast(tmp_path):
    driver = _driver(tmp_path)
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0)
    # Fill the queue to its bound while the worker is gated.
    with ThreadPoolExecutor(max_workers=MT5_JOB_QUEUE_MAX + 4) as pool:
        futures = [pool.submit(driver.run, lambda _m: None, "fill") for _ in range(MT5_JOB_QUEUE_MAX)]
        time.sleep(0.5)
        assert driver.queue_depths()["total"] == MT5_JOB_QUEUE_MAX
        with pytest.raises(BridgeError) as excinfo:
            driver.run(lambda _m: None, op="one-too-many")
        assert excinfo.value.code is ErrorCode.MT5_CALL_FAILED
        assert "job queue full" in excinfo.value.message
        gate.set()
        for fut in futures:
            fut.result(timeout=10.0)
    gate_thread.join(timeout=5.0)


def test_run_order_send_uses_command_lane(tmp_path):
    driver = _driver(tmp_path)
    order: list[str] = []
    gate = threading.Event()
    started = threading.Event()

    def gate_job(_mt5):
        started.set()
        gate.wait(timeout=10.0)
        return "gate"

    gate_thread = _run_in_thread(driver, gate_job, op="gate")
    assert started.wait(timeout=5.0)
    bg = _run_in_thread(driver, lambda _m: order.append("background"), op="bg", priority=MT5_PRIORITY_BACKGROUND)
    send_thread = threading.Thread(
        target=lambda: driver.run_order_send(lambda _m: order.append("order_send"), op="order_send"),
        daemon=True,
    )
    send_thread.start()
    time.sleep(0.2)
    gate.set()
    gate_thread.join(timeout=5.0)
    bg.join(timeout=5.0)
    send_thread.join(timeout=5.0)
    assert order == ["order_send", "background"], "order placement must jump queued background work"


# ----------------------------------------------------------------------
# Per-ticket fence tokens
# ----------------------------------------------------------------------
def test_fence_different_ticket_tokens_are_independent():
    fence = CommandFence()
    barrier = threading.Barrier(2)
    acquired: dict[str, bool] = {}

    def acquire(token: str, command_id: str) -> None:
        barrier.wait(timeout=5.0)
        acquired[command_id] = fence.acquire("dev-1", token, command_id, timeout_s=1.0)

    t1 = threading.Thread(target=acquire, args=("ticket:9000001", "cmd-1"), daemon=True)
    t2 = threading.Thread(target=acquire, args=("ticket:9000002", "cmd-2"), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert acquired == {"cmd-1": True, "cmd-2": True}, "independent tickets must not fence each other"


def test_fence_same_ticket_token_still_serializes():
    fence = CommandFence()
    assert fence.acquire("dev-1", "ticket:9000001", "cmd-1", timeout_s=1.0) is True
    assert fence.acquire("dev-1", "ticket:9000001", "cmd-2", timeout_s=0.0) is False
    fence.release("dev-1", "ticket:9000001", "cmd-1")
    assert fence.acquire("dev-1", "ticket:9000001", "cmd-2", timeout_s=0.0) is True


def test_executor_runs_different_ticket_commands_concurrently():
    executor = _executor()
    calls: list[str] = []
    call_lock = threading.Lock()

    def fake_dispatch(envelope):
        with call_lock:
            calls.append(envelope.ticket)
        time.sleep(0.4)
        return {"ok": True}

    executor._dispatch = fake_dispatch  # type: ignore[method-assign]
    results: dict[str, tuple[object, dict]] = {}

    def run(ticket: str) -> None:
        results[ticket] = executor.execute(_frame(ticket))

    t1 = threading.Thread(target=run, args=("9000001",), daemon=True)
    t2 = threading.Thread(target=run, args=("9000002",), daemon=True)
    t0 = time.monotonic()
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.7, f"different tickets must run concurrently, took {elapsed:.2f}s"
    for ticket in ("9000001", "9000002"):
        execution, result = results[ticket]
        assert execution.state is CommandState.COMPLETED, result
        assert result["status"] == "executed"
    assert sorted(calls) == ["9000001", "9000002"]


def test_executor_dispatch_runs_in_command_lane(tmp_path):
    """The executor must put the whole dispatch in the command lane:
    a close (envelope priority 9) in URGENT, any other command in
    COMMAND — so the lane is set even though no engine helper takes
    a priority argument."""
    executor = _executor()
    seen: dict[str, int] = {}

    def fake_dispatch(envelope):
        seen[envelope.operation_type] = current_mt5_priority()
        return {"ok": True}

    executor._dispatch = fake_dispatch  # type: ignore[method-assign]
    execution, result = executor.execute(_frame("9000001"))
    assert execution.state is CommandState.COMPLETED, result
    assert seen["close_position"] == MT5_PRIORITY_URGENT
    execution, result = executor.execute(
        _frame("9000001", operation={"type": "modify_sl_tp", "ticket": "9000001", "sl": 1.08})
    )
    assert execution.state is CommandState.COMPLETED, result
    assert seen["modify_sl_tp"] == MT5_PRIORITY_COMMAND


def test_executor_fences_same_ticket_command_fail_closed():
    executor = _executor(fence_timeout_s=0.2)
    entered = threading.Event()
    release = threading.Event()

    def fake_dispatch(envelope):
        entered.set()
        assert release.wait(timeout=10.0), "dispatch must be released by the test"
        return {"ok": True}

    executor._dispatch = fake_dispatch  # type: ignore[method-assign]
    results: dict[str, tuple[object, dict]] = {}

    def run(command_id: str) -> None:
        results[command_id] = executor.execute(_frame("9000001", command_id=command_id))

    first_id = new_uuid7_str()
    second_id = new_uuid7_str()
    t1 = threading.Thread(target=run, args=(first_id,), daemon=True)
    t1.start()
    assert entered.wait(timeout=5.0), "first dispatch never started"
    t2 = threading.Thread(target=run, args=(second_id,), daemon=True)
    t2.start()
    t2.join(timeout=10.0)
    assert not t2.is_alive()
    execution, result = results[second_id]
    assert result["status"] == "rejected", result
    assert result["error"]["code"] == RejectionReason.FENCED.value, result
    release.set()
    t1.join(timeout=10.0)
    execution, result = results[first_id]
    assert execution.state is CommandState.COMPLETED, result
    assert result["status"] == "executed"

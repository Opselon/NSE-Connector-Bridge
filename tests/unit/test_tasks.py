"""Unit tests for the task-execution boundary skeleton (Phase 2).

Label: unit. The kind registry starts EMPTY — the server cannot
assign compute until a future contract version registers a kind.
No engines, no backtest, no AI code anywhere in this module.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from nse_bridge.server.uuid7 import new_uuid7_str
from nse_bridge.tasks.envelope import (
    TaskExecution,
    TaskRejection,
    TaskState,
    build_task_result,
    parse_task,
)
from nse_bridge.tasks.registry import TaskKindRegistry
from nse_bridge.tasks.validation import TaskValidator
from nse_bridge.tasks.worker import TaskWorker

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DEVICE = "dev-1"


def _frame(**overrides):
    issued = NOW - timedelta(minutes=1)
    frame = {
        "task_id": new_uuid7_str(),
        "kind": "compute.example",
        "params": {"x": 1},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "same-as-task-id",
        "resource_limits": {"max_cpu_s": 60, "max_memory_mb": 512, "timeout_s": 30},
    }
    frame.update(overrides)
    return frame


@pytest.mark.unit
def test_registry_starts_empty():
    registry = TaskKindRegistry()
    assert registry.kinds() == []
    assert registry.is_registered("compute.example") is False
    assert registry.get("compute.example") is None


@pytest.mark.unit
def test_registry_requires_approval_reference():
    registry = TaskKindRegistry()
    with pytest.raises(ValueError):
        registry.register("compute.example", lambda p: {}, approved_by="")
    registry.register("compute.example", lambda p: {"ok": True}, approved_by="CONTRACT v1.2 §7")
    assert registry.is_registered("compute.example") is True
    with pytest.raises(ValueError):
        registry.register("compute.example", lambda p: {}, approved_by="CONTRACT v1.2 §7")


@pytest.mark.unit
def test_task_validation_unknown_kind_rejected():
    validator = TaskValidator(DEVICE, clock=lambda: NOW)
    outcome = validator.validate(_frame())
    assert outcome.rejection is TaskRejection.UNKNOWN_KIND


@pytest.mark.unit
def test_task_validation_order_malformed_expired_device_account():
    validator = TaskValidator(DEVICE, clock=lambda: NOW)
    assert validator.validate({"task_id": "x"}).rejection is TaskRejection.MALFORMED
    expired = _frame(expires_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
    assert validator.validate(expired).rejection is TaskRejection.EXPIRED
    wrong_device = _frame(device_id="someone-else")
    assert validator.validate(wrong_device).rejection is TaskRejection.WRONG_DEVICE
    validator_bound = TaskValidator(
        DEVICE, get_account_id=lambda: "123", clock=lambda: NOW
    )
    assert validator_bound.validate(_frame(account_id="999")).rejection is TaskRejection.WRONG_ACCOUNT


@pytest.mark.unit
def test_task_validation_replay_and_no_consume_on_failure():
    registry = TaskKindRegistry()
    registry.register("compute.example", lambda p: {"ok": True}, approved_by="test")
    validator = TaskValidator(DEVICE, kinds=registry, clock=lambda: NOW)
    task_id = new_uuid7_str()
    assert validator.validate(_frame(task_id=task_id)).ok is True
    assert validator.validate(_frame(task_id=task_id)).rejection is TaskRejection.REPLAYED
    # An expired frame does NOT consume the id (§6.3 mirror rule).
    expired = _frame(
        task_id=new_uuid7_str(),
        expires_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    assert validator.validate(expired).rejection is TaskRejection.EXPIRED
    fresh = _frame(task_id=expired["task_id"])
    assert validator.validate(fresh).ok is True


@pytest.mark.unit
def test_task_envelope_parse_and_limits():
    envelope = parse_task(_frame())
    assert envelope.kind == "compute.example"
    assert envelope.resource_limits.timeout_s == 30.0
    assert envelope.resource_limits.max_memory_mb == 512
    with pytest.raises(ValueError):
        parse_task(_frame(kind=""))
    with pytest.raises(TypeError):
        parse_task(_frame(params="not-a-dict"))


@pytest.mark.unit
def test_task_result_envelope_mirrors_action_result():
    started = NOW
    completed = NOW + timedelta(milliseconds=10)
    result = build_task_result(
        task_id="t1", status="completed", started_at=started,
        completed_at=completed, result={"answer": 42}, device_id=DEVICE,
    )
    assert result["event_type"] == "task.result"
    assert result["correlation_id"] == "t1"
    assert result["status"] == "completed"
    assert result["result"] == {"answer": 42}
    assert result["duration_ms"] == pytest.approx(10.0)
    assert result["protocol_version"] == "1.0"


@pytest.mark.unit
def test_worker_runs_registered_kind_and_reports_completed():
    registry = TaskKindRegistry()
    registry.register("compute.example", lambda p: {"doubled": p["x"] * 2}, approved_by="test")
    validator = TaskValidator(DEVICE, kinds=registry, clock=lambda: NOW)
    worker = TaskWorker(validator, kinds=registry, device_id=DEVICE)
    try:
        frame = _frame()
        execution, queued = worker.submit(frame)
        assert queued["queued"] is True
        assert execution.state is TaskState.EXECUTING
        result = worker._run_one(parse_task(frame))
        assert result["status"] == "completed"
        assert result["result"] == {"doubled": 2}
        assert execution.state is TaskState.COMPLETED
        assert execution.result is result
    finally:
        worker.stop()


@pytest.mark.unit
def test_worker_rejects_unregistered_kind_without_executing():
    registry = TaskKindRegistry()
    validator = TaskValidator(DEVICE, kinds=registry, clock=lambda: NOW)
    worker = TaskWorker(validator, kinds=registry, device_id=DEVICE)
    try:
        execution, result = worker.submit(_frame())
        assert execution.state is TaskState.FAILED
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "UnknownKind"
    finally:
        worker.stop()


@pytest.mark.unit
def test_worker_handler_failure_is_failed_not_crash():
    registry = TaskKindRegistry()

    def boom(params):
        raise RuntimeError("kaboom")

    registry.register("compute.example", boom, approved_by="test")
    validator = TaskValidator(DEVICE, kinds=registry, clock=lambda: NOW)
    worker = TaskWorker(validator, kinds=registry, device_id=DEVICE)
    try:
        frame = _frame()
        _, _queued = worker.submit(frame)
        result = worker._run_one(parse_task(frame))
        assert result["status"] == "failed"
        assert result["error"]["code"] == "TaskFailed"
        assert result["error"]["retry_safe"] is False
    finally:
        worker.stop()


@pytest.mark.unit
def test_worker_timeout_enforced():
    registry = TaskKindRegistry()
    registry.register("compute.example", lambda p: time.sleep(2), approved_by="test")
    validator = TaskValidator(DEVICE, kinds=registry, clock=lambda: NOW)
    worker = TaskWorker(validator, kinds=registry, device_id=DEVICE)
    try:
        frame = _frame()
        frame["resource_limits"] = {"max_cpu_s": 60, "max_memory_mb": 512, "timeout_s": 0.2}
        _, _queued = worker.submit(frame)
        result = worker._run_one(parse_task(frame))
        assert result["status"] == "failed"
        assert result["error"]["code"] == "TaskTimeout"
    finally:
        worker.stop()


@pytest.mark.unit
def test_task_lifecycle_illegal_transition():
    ex = TaskExecution(task_id="t1")
    with pytest.raises(ValueError):
        ex.transition(TaskState.COMPLETED)  # RECEIVED → COMPLETED is illegal

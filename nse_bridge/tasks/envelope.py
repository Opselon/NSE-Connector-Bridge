"""Task envelope + lifecycle + result envelope (skeleton, Phase 2).

Generic envelope (mirrors the §6.2 command envelope)::

    {
      "task_id": "<uuidv7>",
      "kind": "<registered kind, e.g. 'compute.xxx' — registry starts EMPTY>",
      "params": {...},
      "issued_at": "<RFC3339>",
      "expires_at": "<RFC3339>",
      "idempotency_key": "<== task_id in v1>",
      "resource_limits": {"max_cpu_s": 60, "max_memory_mb": 512, "timeout_s": 30}
    }

Lifecycle mirrors the command lifecycle (clarification #2):
RECEIVED → VALIDATED → AUTHORIZED → DISPATCHED → EXECUTING →
COMPLETED | FAILED | UNKNOWN | EXPIRED. UNKNOWN = result uncertain —
reconcile before any retry; never blind-retry a non-idempotent task.

Result envelope mirrors ``action.result`` (§6.5): ``task.result``
with status ``completed | failed | rejected``, ``correlation_id`` =
task_id, client-side duration, and a ``retry_safe`` flag.

WIRE FORMAT WARNING: this format is NOT in CONTRACT v1. It is a
local extension point whose wire format needs a future contract
amendment. Nothing in this module is ever sent over the wire today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ..server.uuid7 import new_uuid7_str
from ..timeutil import normalize_utc

__all__ = [
    "ResourceLimits",
    "TaskEnvelope",
    "TaskExecution",
    "TaskRejection",
    "TaskResult",
    "TaskState",
    "build_task_result",
    "parse_task",
    "rejection_task_result",
]


class TaskRejection(str, Enum):
    MALFORMED = "Malformed"
    EXPIRED = "Expired"
    WRONG_DEVICE = "WrongDevice"
    WRONG_ACCOUNT = "WrongAccount"
    UNKNOWN_KIND = "UnknownKind"
    REPLAYED = "Replayed"


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


LEGAL_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset({TaskState.VALIDATED, TaskState.FAILED, TaskState.EXPIRED}),
    TaskState.VALIDATED: frozenset({TaskState.AUTHORIZED, TaskState.FAILED}),
    TaskState.AUTHORIZED: frozenset({TaskState.DISPATCHED, TaskState.FAILED}),
    TaskState.DISPATCHED: frozenset({TaskState.EXECUTING, TaskState.FAILED, TaskState.EXPIRED}),
    TaskState.EXECUTING: frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.UNKNOWN}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.UNKNOWN: frozenset(),
    TaskState.EXPIRED: frozenset(),
}


@dataclass
class ResourceLimits:
    """Resource limits for one task.

    ``timeout_s`` is enforced by the worker (hard). ``max_cpu_s`` /
    ``max_memory_mb`` are RECORDED limits: they cannot be enforced
    in-process on Linux/Windows without a sandbox/cgroup, so a future
    contract version must define the enforcement mechanism before any
    kind that needs them is registered. No task runs today.
    """

    max_cpu_s: float = 60.0
    max_memory_mb: int = 512
    timeout_s: float = 30.0


@dataclass
class TaskEnvelope:
    task_id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    issued_at: str | None = None
    expires_at: str | None = None
    idempotency_key: str | None = None
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    device_id: str | None = None
    account_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_task(frame: Any) -> TaskEnvelope:
    """Parse a task frame or raise ValueError (malformed)."""
    if not isinstance(frame, dict):
        raise TypeError("task frame is not a JSON object")
    for key in ("task_id", "kind", "issued_at", "expires_at"):
        if frame.get(key) in (None, ""):
            raise ValueError(f"task frame is missing {key!r}")
    if normalize_utc(frame.get("expires_at"), 0) is None:
        raise ValueError("task expires_at is not RFC 3339")
    if normalize_utc(frame.get("issued_at"), 0) is None:
        raise ValueError("task issued_at is not RFC 3339")
    params = frame.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise TypeError("task params is not an object")
    limits_raw = frame.get("resource_limits", {})
    if limits_raw is None:
        limits_raw = {}
    if not isinstance(limits_raw, dict):
        raise TypeError("task resource_limits is not an object")
    limits = ResourceLimits(
        max_cpu_s=float(limits_raw.get("max_cpu_s", 60.0)),
        max_memory_mb=int(limits_raw.get("max_memory_mb", 512)),
        timeout_s=float(limits_raw.get("timeout_s", 30.0)),
    )
    return TaskEnvelope(
        task_id=str(frame["task_id"]),
        kind=str(frame["kind"]),
        params=params,
        issued_at=str(frame["issued_at"]),
        expires_at=str(frame["expires_at"]),
        idempotency_key=str(frame["idempotency_key"]) if frame.get("idempotency_key") is not None else None,
        resource_limits=limits,
        device_id=str(frame["device_id"]) if frame.get("device_id") is not None else None,
        account_id=str(frame["account_id"]) if frame.get("account_id") is not None else None,
        raw=frame,
    )


@dataclass
class TaskExecution:
    task_id: str
    state: TaskState = TaskState.RECEIVED
    history: list[tuple[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.history.append((self.state.value, datetime.now(UTC).isoformat()))

    def transition(self, new_state: TaskState) -> None:
        if new_state not in LEGAL_TRANSITIONS[self.state]:
            raise ValueError(f"illegal task transition {self.state.value} → {new_state.value} for task {self.task_id}")
        self.state = new_state
        self.history.append((new_state.value, datetime.now(UTC).isoformat()))


@dataclass
class TaskResult:
    """Result envelope mirroring ``action.result`` (§6.5)."""

    task_id: str
    status: str  # completed | failed | rejected
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def build_task_result(
    *,
    task_id: str,
    status: str,
    started_at: datetime,
    completed_at: datetime,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Build a ``task.result`` envelope (mirrors §6.5 ``action.result``)."""
    duration_ms = max(0.0, (completed_at - started_at).total_seconds() * 1000.0)
    envelope: dict[str, Any] = {
        "protocol_version": "1.0",
        "schema_version": "2026-11-17",
        "message_id": new_uuid7_str(),
        "correlation_id": task_id,
        "event_type": "task.result",
        "event_timestamp": completed_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": round(duration_ms, 2),
        "result": result or {},
    }
    if device_id:
        envelope["device_id"] = device_id
    if error is not None:
        envelope["error"] = error
    return envelope


def rejection_task_result(
    *,
    task_id: str,
    reason: TaskRejection,
    started_at: datetime | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return build_task_result(
        task_id=task_id,
        status="rejected",
        started_at=started_at or now,
        completed_at=now,
        device_id=device_id,
        error={
            "code": reason.value,
            "category": "Validation",
            "cause": f"task rejected at validation: {reason.value}",
            "retry_safe": False,
        },
    )

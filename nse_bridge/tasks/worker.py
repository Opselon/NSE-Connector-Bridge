"""Bounded task worker (skeleton, Phase 2).

Executes validated tasks from APPROVED/TRUSTED registered modules
only, in a bounded thread pool (default 1 worker, bounded queue of
32). The worker enforces the envelope's ``timeout_s`` (hard, via the
future timeout); ``max_cpu_s`` / ``max_memory_mb`` are recorded, not
enforced in-process (see :class:`nse_bridge.tasks.envelope.ResourceLimits`).

Unknown kinds never reach the worker: validation rejects them first.
A task that raises, times out, or returns a non-dict result fails
with ``status: "failed"`` and ``retry_safe: False``. The worker is
NOT wired to the WS command path.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from .envelope import (
    TaskEnvelope,
    TaskExecution,
    TaskRejection,
    TaskState,
    build_task_result,
    rejection_task_result,
)
from .registry import TaskKindRegistry
from .validation import TaskValidator

__all__ = ["TaskWorker"]

DEFAULT_MAX_WORKERS = 1
DEFAULT_MAX_QUEUE = 32


class TaskWorker:
    """Bounded worker for validated, registered task kinds."""

    def __init__(
        self,
        validator: TaskValidator,
        *,
        kinds: TaskKindRegistry | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_queue: int = DEFAULT_MAX_QUEUE,
        device_id: str | None = None,
    ) -> None:
        self.validator = validator
        self.kinds = kinds or validator.kinds
        self.device_id = device_id
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="nse-task-worker"
        )
        self._queue: queue.Queue[TaskEnvelope] = queue.Queue(maxsize=max_queue)
        self._pending: dict[str, TaskExecution] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._drain, name="nse-task-drain", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def submit(self, frame: Any) -> tuple[TaskExecution, dict[str, Any]]:
        """Validate and enqueue a task frame.

        Rejections return immediately (never queued, never executed).
        Accepted tasks are queued (bounded); the drain loop completes
        their lifecycle (``execution`` is updated in place).
        """
        started = datetime.now(UTC)
        outcome = self.validator.validate(frame)
        if not outcome.ok:
            reason = outcome.rejection or TaskRejection.MALFORMED
            task_id = frame.get("task_id") if isinstance(frame, dict) else None
            task_id = str(task_id) if task_id else "unknown"
            execution = TaskExecution(task_id=task_id)
            execution.transition(
                TaskState.EXPIRED if reason is TaskRejection.EXPIRED else TaskState.FAILED
            )
            result = rejection_task_result(
                task_id=task_id, reason=reason, started_at=started, device_id=self.device_id
            )
            execution.result = result
            return execution, result
        envelope = outcome.envelope
        assert envelope is not None
        execution = TaskExecution(task_id=envelope.task_id)
        execution.transition(TaskState.VALIDATED)
        execution.transition(TaskState.AUTHORIZED)
        execution.transition(TaskState.DISPATCHED)
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            logger.warning("[TASK] task queue full — rejecting {}", envelope.task_id)
            execution.transition(TaskState.FAILED)
            result = build_task_result(
                task_id=envelope.task_id, status="failed", started_at=started,
                completed_at=datetime.now(UTC), device_id=self.device_id,
                error={
                    "code": "TaskQueueFull",
                    "category": "Execution",
                    "cause": "task queue is full (bounded at 32)",
                    "retry_safe": False,
                },
            )
            execution.result = result
            return execution, result
        execution.transition(TaskState.EXECUTING)
        self._pending[envelope.task_id] = execution
        return execution, {"queued": True, "task_id": envelope.task_id}

    # ------------------------------------------------------------------
    def _drain(self) -> None:
        while self._running:
            try:
                envelope = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._run_one(envelope)

    def _run_one(self, envelope: TaskEnvelope) -> dict[str, Any]:
        """Execute one validated task synchronously; returns its result."""
        started = datetime.now(UTC)
        execution = self._pending.pop(envelope.task_id, None)
        handler = self.kinds.get(envelope.kind)
        if handler is None:  # defence in depth — validation already rejected this
            result = rejection_task_result(
                task_id=envelope.task_id, reason=TaskRejection.UNKNOWN_KIND,
                started_at=started, device_id=self.device_id,
            )
            if execution is not None:
                execution.transition(TaskState.FAILED)
                execution.result = result
            return result
        timeout = max(0.1, float(envelope.resource_limits.timeout_s))
        try:
            future = self._executor.submit(handler, dict(envelope.params))
            output = future.result(timeout=timeout)
            if not isinstance(output, dict):
                raise TypeError("task handler must return a dict")
        except FutureTimeoutError:
            result = self._failed(envelope, started, "TaskTimeout",
                                  f"task exceeded its timeout_s={timeout}")
            self._settle(execution, TaskState.FAILED, result)
            return result
        except Exception as exc:  # noqa: BLE001 — a failing task is failed, never a crash
            result = self._failed(envelope, started, "TaskFailed",
                                  f"task handler raised {type(exc).__name__}")
            self._settle(execution, TaskState.FAILED, result)
            return result
        result = build_task_result(
            task_id=envelope.task_id, status="completed", started_at=started,
            completed_at=datetime.now(UTC), result=output, device_id=self.device_id,
        )
        self._settle(execution, TaskState.COMPLETED, result)
        return result

    # ------------------------------------------------------------------
    def _failed(
        self, envelope: TaskEnvelope, started: datetime, code: str, cause: str
    ) -> dict[str, Any]:
        return build_task_result(
            task_id=envelope.task_id, status="failed", started_at=started,
            completed_at=datetime.now(UTC), device_id=self.device_id,
            error={"code": code, "category": "Execution", "cause": cause, "retry_safe": False},
        )

    @staticmethod
    def _settle(
        execution: TaskExecution | None, state: TaskState, result: dict[str, Any]
    ) -> None:
        if execution is not None:
            execution.transition(state)
            execution.result = result

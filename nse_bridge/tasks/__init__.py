"""Task-execution boundary — SKELETON ONLY (Phase 2).

Future server-assigned local compute may run locally to distribute
load, but NO engines exist now and none are built here: no indicator
engines, no AI agents, no LLM, no backtest, no risk engines, no
position sizing, no coaching, no optimizers.

The boundary is a generic envelope + validation + bounded worker +
result envelope, mirroring the command path (§6.3 validation order,
``action.result`` result shape). The kind registry starts EMPTY —
the server cannot assign compute until a future contract version
registers a kind. A kind maps to a registered, reviewed local module
only (approved/trusted modules); NEVER arbitrary remote code
execution, never download-and-run untrusted code.

WIRE FORMAT WARNING: the task wire format is NOT in CONTRACT v1.
This package is a LOCAL extension point whose wire format needs a
future contract amendment before any task is ever sent over the
wire. It is NOT wired to the WS command path.
"""

from __future__ import annotations

from .envelope import (
    ResourceLimits,
    TaskEnvelope,
    TaskExecution,
    TaskRejection,
    TaskResult,
    TaskState,
    build_task_result,
    parse_task,
    rejection_task_result,
)
from .registry import TaskKindRegistry, registry
from .validation import TaskValidator
from .worker import TaskWorker

__all__ = [
    "ResourceLimits",
    "TaskEnvelope",
    "TaskExecution",
    "TaskKindRegistry",
    "TaskRejection",
    "TaskResult",
    "TaskState",
    "TaskValidator",
    "TaskWorker",
    "build_task_result",
    "parse_task",
    "registry",
    "rejection_task_result",
]

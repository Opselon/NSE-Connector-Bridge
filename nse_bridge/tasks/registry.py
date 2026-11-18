"""Task kind registry — starts EMPTY (Phase 2 skeleton).

A kind maps to a registered, reviewed LOCAL module (approved/trusted
modules only). The server cannot assign compute until a future
contract version registers a kind: an unregistered kind is rejected
at validation (``UnknownKind``) and never executed.

Registration is explicit code, never data-driven: a kind is added by
a reviewed change that imports the module and calls
:meth:`TaskKindRegistry.register`. There is no dynamic import, no
``eval``/``exec``, no download-and-run — arbitrary remote code
execution is impossible by construction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["TaskKindRegistry", "registry"]

#: A kind handler: ``handler(params: dict) -> dict`` (synchronous;
#: the worker runs it in a bounded thread pool).
TaskHandler = Callable[[dict[str, Any]], dict[str, Any]]


class TaskKindRegistry:
    """Explicit registry of approved local compute kinds."""

    def __init__(self) -> None:
        self._kinds: dict[str, TaskHandler] = {}

    def register(self, kind: str, handler: TaskHandler, *, approved_by: str) -> None:
        """Register a kind. ``approved_by`` names the review/contract
        version that approved it (e.g. ``"CONTRACT v1.2 §7"``) — an
        empty approval is refused: no silent registrations."""
        if not kind or not isinstance(kind, str):
            raise ValueError("task kind must be a non-empty string")
        if not approved_by or not isinstance(approved_by, str):
            raise ValueError("task kind registration requires an approval reference")
        if kind in self._kinds:
            raise ValueError(f"task kind {kind!r} is already registered")
        self._kinds[kind] = handler

    def is_registered(self, kind: str) -> bool:
        return kind in self._kinds

    def get(self, kind: str) -> TaskHandler | None:
        return self._kinds.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._kinds)


#: The process-wide registry. Starts EMPTY — no kinds are registered
#: in this phase, by design.
registry = TaskKindRegistry()

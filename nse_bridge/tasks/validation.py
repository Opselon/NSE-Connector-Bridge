"""Task validation — mirrors the §6.3 command validation order.

Order (exact): malformed → expired → wrong-device → wrong-account →
kind-allowlist (``UnknownKind`` — the registry starts EMPTY) → replay.
Only a SUCCESSFUL validation consumes the replay id. The replay cache
is bounded at 10,000 ids, mirroring the command path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..server.commands import ReplayCache
from ..timeutil import normalize_utc
from .envelope import TaskEnvelope, TaskRejection, parse_task
from .registry import TaskKindRegistry

__all__ = ["TaskValidationOutcome", "TaskValidator"]


@dataclass
class TaskValidationOutcome:
    envelope: TaskEnvelope | None = None
    rejection: TaskRejection | None = None

    @property
    def ok(self) -> bool:
        return self.envelope is not None and self.rejection is None


class TaskValidator:
    def __init__(
        self,
        device_id: str,
        *,
        get_account_id: Callable[[], str | None] | None = None,
        kinds: TaskKindRegistry | None = None,
        replay_cache: ReplayCache | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.device_id = device_id
        self._get_account_id = get_account_id or (lambda: None)
        self.kinds = kinds or TaskKindRegistry()
        self.replay_cache = replay_cache or ReplayCache()
        self._clock = clock or (lambda: datetime.now(UTC))

    def validate(self, frame: object) -> TaskValidationOutcome:
        # 1 — malformed
        try:
            envelope = parse_task(frame)
        except ValueError:
            return TaskValidationOutcome(rejection=TaskRejection.MALFORMED)
        # 2 — expired
        expires = normalize_utc(envelope.expires_at, 0)
        assert expires is not None
        if expires <= self._clock():
            return TaskValidationOutcome(rejection=TaskRejection.EXPIRED)
        # 3 — wrong device (only when the frame names a device)
        if envelope.device_id is not None and envelope.device_id != self.device_id:
            return TaskValidationOutcome(rejection=TaskRejection.WRONG_DEVICE)
        # 4 — wrong account (only when the frame names an account)
        if envelope.account_id is not None:
            bound = self._get_account_id()
            if bound is None or envelope.account_id != bound:
                return TaskValidationOutcome(rejection=TaskRejection.WRONG_ACCOUNT)
        # 5 — kind allowlist (registry starts EMPTY → everything rejected)
        if not self.kinds.is_registered(envelope.kind):
            return TaskValidationOutcome(rejection=TaskRejection.UNKNOWN_KIND)
        # 6 — replay (only a SUCCESSFUL validation consumes the id)
        if envelope.task_id in self.replay_cache:
            return TaskValidationOutcome(rejection=TaskRejection.REPLAYED)
        self.replay_cache.consume(envelope.task_id)
        return TaskValidationOutcome(envelope=envelope)

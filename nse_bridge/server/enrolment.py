"""Device enrolment at startup (CONTRACT v1 §3.1/§3.2, §5.1).

Phase 2 built the REST client (``validate-key``, ``register-device``)
and the WS channel, but nothing in the running app ever called them
at startup — the channel connected with headers directly. This module
closes that gap: enrolment is the state machine that establishes the
device identity BEFORE the WS channel may open (§5.1: "Device
enrolled first via REST").

States::

    NOT_CONFIGURED  — no server key / no server URL configured;
                      enrolment disabled (honest state, not an error).
    VALIDATING      — a validate-key → register-device attempt is in
                      flight (startup enrolment or re-enrolment).
    REGISTERED      — validate-key + register-device both succeeded;
                      the WS channel may open; ``session_expires_at``
                      is the live session expiry from the server.
    FAILED_AUTH     — 401 ``key_invalid``/``key_revoked`` or 403 on
                      either call: TERMINAL for this key. No retry
                      with the same key, no unauthenticated fallback,
                      sync stops immediately (§3.1). Only a new key
                      (``reset_for_new_key``) leaves this state.
    FAILED_TRANSIENT — 5xx / 429 / network error: retry with backoff
                      (500 ms doubling → 30 s + jitter, §5.4).

Re-enrolment after a 401 (§3.2): Phase 2's :mod:`nse_bridge.server.rest`
already implements the recovery path — a 401 on a non-auth endpoint
re-enrols via ``register-device`` ONCE, then retries the original call
once; if ``register-device`` itself 401s the error propagates. This
manager does NOT duplicate that path: the REST client notifies it
(``note_reauth_*``) so the state machine reflects the outcome
(``REGISTERED`` again, or ``FAILED_AUTH`` when the key itself is bad).
The full ``validate-key → register-device`` sequence is the startup
enrolment path implemented here (``enrol_once``).

The manager supervises continuously (``run``): it parks while
``REGISTERED`` (woken by a re-auth notification), retries transient
failures with backoff, parks while unconfigured (re-checking every
few seconds so a key entered at runtime via Settings takes effect),
and exits on ``FAILED_AUTH`` or when the stop event is set. It never
blocks app start: the lifespan runs it as a background task, and the
local GUI + MT5 keep working while the Platform is down.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from .errors import ServerError
from .manifest import ManifestTracker, build_manifest

if TYPE_CHECKING:
    from ..engine.common import BridgeContext
    from .rest import RestClient, ValidateKeyResult

__all__ = [
    "EnrolmentManager",
    "EnrolmentState",
    "EnrolmentStatus",
    "enrolment_backoff_delay",
]

#: §5.4 reconnect schedule, reused for enrolment retries: 500 ms
#: doubling, capped at 30 s, + 0–100 ms jitter.
ENROLMENT_BACKOFF_INITIAL_S = 0.5
ENROLMENT_BACKOFF_CAP_S = 30.0
ENROLMENT_BACKOFF_FACTOR = 2.0
ENROLMENT_JITTER_MAX_S = 0.1
#: How often the supervisor re-checks for configuration while
#: NOT_CONFIGURED (a key entered at runtime via Settings applies
#: without a bridge restart).
NOT_CONFIGURED_POLL_S = 5.0


class EnrolmentState(str):
    """Enrolment states (see the module docstring for the transitions)."""

    NOT_CONFIGURED = "not_configured"
    VALIDATING = "validating"
    REGISTERED = "registered"
    FAILED_AUTH = "failed_auth"
    FAILED_TRANSIENT = "failed_transient"


def enrolment_backoff_delay(
    attempt: int,
    *,
    initial_s: float = ENROLMENT_BACKOFF_INITIAL_S,
    cap_s: float = ENROLMENT_BACKOFF_CAP_S,
    factor: float = ENROLMENT_BACKOFF_FACTOR,
) -> float:
    """§5.4 backoff: ``initial``, doubling, capped at ``cap``.

    ``attempt`` counts consecutive transient failures (0 = first retry).
    """
    return min(initial_s * (factor**attempt), cap_s)


@dataclass
class EnrolmentStatus:
    """Snapshot of the enrolment state, for the GUI + diagnostics."""

    state: str = EnrolmentState.NOT_CONFIGURED
    configured: bool = False
    last_error: str | None = None  # contract §3.6 error code, e.g. "key_invalid"
    last_error_message: str | None = None  # server message, truncated; never key material
    attempts: int = 0  # validate-key → register-device attempts so far


class EnrolmentManager:
    """Owns the enrolment state machine for one REST client.

    Backoff delays actually waited are recorded on
    :attr:`backoff_delays` so tests can observe the §5.4 schedule
    without waiting on real sleeps.
    """

    def __init__(
        self,
        ctx: BridgeContext,
        rest_client: RestClient,
        *,
        retry_base_s: float = ENROLMENT_BACKOFF_INITIAL_S,
        retry_cap_s: float = ENROLMENT_BACKOFF_CAP_S,
        jitter: bool = True,
        telemetry: Any | None = None,
        manifest_tracker: ManifestTracker | None = None,
    ) -> None:
        self._ctx = ctx
        self._rest = rest_client
        self._retry_base_s = retry_base_s
        self._retry_cap_s = retry_cap_s
        self._jitter = jitter
        # TelemetryEmitter (ARCHITECTURE-CONTRACT §8), optional.
        self.telemetry = telemetry if telemetry is not None else getattr(ctx, "telemetry", None)
        # Manifest tracker (§4.1): shared with the WS channel so the
        # REST post here and the WS ``manifest.update`` frame there
        # agree on what the server already has.
        self.manifest_tracker = manifest_tracker or ManifestTracker()
        self._status = EnrolmentStatus(configured=self.is_configured)
        self._transient_failures = 0
        self.backoff_delays: list[float] = []
        # Per-waiter state events: every state transition wakes all
        # current waiters (the WS channel gate, the supervisor's park
        # loop). The old 100 ms state polls burned ~40k wakeups/hour
        # while idle; events wake exactly on transition. Per-waiter
        # (not one shared Event) so a clearing waiter can never swallow
        # another's wake — _set_state is sync and can signal safely.
        self._state_waiters: set[asyncio.Event] = set()
        # In-flight best-effort manifest POST (§4.1): enrol_once fires
        # it as a background task (see there) so a slow/hung POST can
        # never hold enrolment completion — or supervisor shutdown —
        # hostage. Kept referenced so it is never GC'd mid-flight.
        self._manifest_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._status.state

    @property
    def is_registered(self) -> bool:
        return self._status.state == EnrolmentState.REGISTERED

    @property
    def is_configured(self) -> bool:
        """A key AND a server base URL must be configured (§3.1)."""
        token = self._ctx.settings.server_token or getattr(self._rest, "api_key", "")
        return bool(token) and bool(self._ctx.settings.server_base_url)

    @property
    def session_expires_at(self) -> str | None:
        return getattr(self._rest, "session_expires_at", None)

    def to_dict(self) -> dict[str, Any]:
        """Diagnostics/GUI payload (no key material, ever)."""
        status = self._status
        return {
            "state": status.state,
            "configured": self.is_configured,
            "last_error": status.last_error,
            "last_error_message": status.last_error_message,
            "attempts": status.attempts,
            "session_expires_at": self.session_expires_at,
            "user_id": getattr(self._rest, "user_id", None),
        }

    # ------------------------------------------------------------------
    def _set_state(
        self,
        state: str,
        *,
        last_error: str | None = None,
        last_error_message: str | None = None,
    ) -> None:
        previous = self._status.state
        self._status.state = state
        self._status.configured = self.is_configured
        for waiter in list(self._state_waiters):
            waiter.set()
        if last_error is not None or state in (
            EnrolmentState.REGISTERED,
            EnrolmentState.VALIDATING,
            EnrolmentState.NOT_CONFIGURED,
        ):
            # A fresh attempt / success / unconfigured clears the old error.
            self._status.last_error = last_error
            self._status.last_error_message = last_error_message
        # Structured telemetry on state changes only (INFO is local-only,
        # WARN/ERROR are queued — §8). No event spam for repeated sets.
        if previous != state and self.telemetry is not None:
            if state == EnrolmentState.REGISTERED:
                self.telemetry.info(
                    "enrolment.registered",
                    component="server.enrolment",
                    details={"attempts": self._status.attempts},
                )
            elif state == EnrolmentState.FAILED_AUTH:
                self.telemetry.error(
                    "enrolment.failed_auth",
                    component="server.enrolment",
                    error_code=last_error,
                    details={"terminal": True, "retry": False},
                )
            elif state == EnrolmentState.FAILED_TRANSIENT:
                self.telemetry.warn(
                    "enrolment.failed_transient",
                    component="server.enrolment",
                    error_code=last_error,
                    details={"attempts": self._status.attempts},
                )

    async def _wait_or_stop(self, stop: asyncio.Event, delay: float) -> None:
        """Wait ``delay`` seconds or until ``stop`` is set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, delay))
        except TimeoutError:
            pass

    def _next_backoff_delay(self) -> float:
        delay = enrolment_backoff_delay(
            max(0, self._transient_failures - 1),
            initial_s=self._retry_base_s,
            cap_s=self._retry_cap_s,
        )
        if self._jitter:
            delay += random.uniform(0, ENROLMENT_JITTER_MAX_S)
        return delay

    # ------------------------------------------------------------------
    def _classify_failure(self, exc: BaseException, step: str) -> bool:
        """Record a failed enrolment step. Returns False (never enrolled).

        Fatal auth (401/403, including a malformed 401/403 response —
        fail closed, §3.6) → FAILED_AUTH, terminal for this key.
        Anything else retryable → FAILED_TRANSIENT.
        """
        if isinstance(exc, ServerError):
            code = exc.code
            if exc.is_fatal_auth or exc.http_status in (401, 403):
                self._set_state(
                    EnrolmentState.FAILED_AUTH,
                    last_error=code.value,
                    last_error_message=(exc.message or "")[:200],
                )
                logger.error(
                    "[ENROL] {} failed: {} — terminal for this key, sync stopped, no retry (§3.1)",
                    step,
                    code.value,
                )
                return False
            self._transient_failures += 1
            self._set_state(
                EnrolmentState.FAILED_TRANSIENT,
                last_error=code.value,
                last_error_message=(exc.message or "")[:200],
            )
            logger.warning(
                "[ENROL] {} failed ({}: {}) — retrying with backoff",
                step,
                code.value,
                (exc.message or "")[:120],
            )
            return False
        self._transient_failures += 1
        self._set_state(
            EnrolmentState.FAILED_TRANSIENT,
            last_error="unexpected_error",
            last_error_message=type(exc).__name__[:200],
        )
        logger.warning("[ENROL] {} failed unexpectedly: {} — retrying with backoff", step, type(exc).__name__)
        return False

    def _store_validate_result(self, result: ValidateKeyResult) -> None:
        """Store the server-resolved subscription/entitlements (§3.1).

        Same shape the GUI refresh action stores, so the Account &
        Subscription screen reads one source of truth. Never key
        material — only server-resolved identity + entitlements.
        """
        self._ctx.validate_key_result = {
            "valid": True,
            "user_id": result.user_id,
            "subscription": dict(result.subscription or {}),
            "entitlements": asdict(result.entitlements),
            "validated_at": datetime.now(UTC).isoformat(),
        }

    async def enrol_once(self) -> bool:
        """One enrolment attempt: validate-key → register-device.

        Returns True when the device is REGISTERED. Never raises for
        a server-side failure — the state machine records it.
        """
        if not self.is_configured:
            self._set_state(EnrolmentState.NOT_CONFIGURED)
            return False
        self._status.attempts += 1
        self._set_state(EnrolmentState.VALIDATING)
        try:
            result = await self._rest.validate_key()
        except Exception as exc:  # noqa: BLE001 — enrolment must never crash the supervisor
            return self._classify_failure(exc, "validate-key")
        self._store_validate_result(result)
        try:
            await self._rest.register_device()
        except Exception as exc:  # noqa: BLE001 — enrolment must never crash the supervisor
            return self._classify_failure(exc, "register-device")
        self._transient_failures = 0
        self._set_state(EnrolmentState.REGISTERED)
        logger.info(
            "[ENROL] device enrolled (user_id={}, session_expires_at={})",
            self._rest.user_id,
            self.session_expires_at,
        )
        # §4.1: POST the action manifest once enrolled (unchanged-hash
        # no-op via the tracker). Best-effort: a manifest failure must
        # NOT un-enrol the device — the manifest is retried on the next
        # change / re-enrolment, and the WS channel also reports it.
        # It runs as a BACKGROUND task, not awaited here: a slow or hung
        # manifest POST (30 s read budget + retries) used to hold
        # enrolment completion hostage, and the supervisor's run() could
        # not park — on ubuntu-24.04-arm the POST stalled >15 s and the
        # enrolment test's teardown wait died on it. REGISTERED is
        # already set above, so nothing downstream waits on the POST.
        if self._manifest_task is None or self._manifest_task.done():
            self._manifest_task = asyncio.create_task(self._publish_manifest())
        return True

    async def _publish_manifest(self) -> None:
        """POST the §4.1 manifest if its hash changed. Never raises."""
        try:
            manifest = build_manifest(
                self._ctx.get_or_create_device_id(),
                driver=getattr(self._ctx, "driver", None),
            )
            await self.manifest_tracker.send_via_rest(self._rest, manifest)
        except Exception as exc:  # noqa: BLE001 — manifest must never break enrolment
            logger.warning("[ENROL] manifest post failed ({}); will retry on next change", type(exc).__name__)

    async def wait_for_manifest(self) -> None:
        """Await the in-flight best-effort manifest POST, if any.

        Tests use this to assert on manifest delivery after enrol_once;
        production never needs it (the POST is fire-and-forget).
        """
        task = self._manifest_task
        if task is not None:
            await task

    async def _cancel_manifest_task(self) -> None:
        """Cancel a still-running best-effort manifest POST (shutdown).

        The POST must not outlive the supervisor: cancelling it keeps
        shutdown prompt and leaves no task pending at interpreter exit.
        """
        task = self._manifest_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Re-enrolment notifications from the REST client (§3.2). The
    # client performs the register-device call itself (Phase 2); this
    # manager only mirrors the outcome into the state machine.
    # ------------------------------------------------------------------
    def note_reauth_start(self) -> None:
        """A 401 on a non-auth endpoint started a re-enrolment."""
        if self._status.state == EnrolmentState.REGISTERED:
            self._set_state(EnrolmentState.VALIDATING)
            logger.warning("[ENROL] 401 on a REST call — re-enrolling this device (§3.2)")

    def note_reauth_succeeded(self) -> None:
        """The re-enrolment's register-device succeeded."""
        self._transient_failures = 0
        self._set_state(EnrolmentState.REGISTERED)
        logger.info("[ENROL] re-enrolment succeeded — device registered again")
        # The §3.2 recovery path registers the device WITHOUT running
        # validate-key, so the cached user_id / subscription /
        # entitlements stayed empty and the Account & Subscription
        # screen read "Not validated yet" forever after a mid-session
        # 401 recovery (observed 2026-11-21). Refresh the validation
        # cache now — best-effort: a failure here is surfaced by the
        # next manual refresh, never breaks the recovery.
        asyncio.get_running_loop().create_task(self._refresh_validation_cache())

    async def _refresh_validation_cache(self) -> None:
        """Run validate-key once after a re-auth so the GUI's cached
        entitlements match the freshly-registered state."""
        try:
            result = await self._rest.validate_key()
            self._ctx.validate_key_result = {
                "valid": True,
                "user_id": result.user_id,
                "subscription": dict(result.subscription or {}),
                "entitlements": {
                    "history_sync": result.entitlements.history_sync,
                    "ai_coach": result.entitlements.ai_coach,
                    "position_manager": result.entitlements.position_manager,
                    "auto_trade": result.entitlements.auto_trade,
                    "max_devices": result.entitlements.max_devices,
                    "max_accounts": result.entitlements.max_accounts,
                },
            }
            logger.info(
                "[ENROL] post-reauth validate-key OK — entitlements cached (user={})",
                result.user_id,
            )
        except Exception as exc:  # noqa: BLE001 — cache refresh is best-effort
            logger.warning("[ENROL] post-reauth validate-key failed: {}", type(exc).__name__)

    def note_reauth_failed(self, exc: ServerError) -> None:
        """The re-enrolment's register-device failed."""
        self._classify_failure(exc, "register-device (re-enrolment)")

    # ------------------------------------------------------------------
    def reset_for_new_key(self) -> None:
        """A new API key was configured (Settings) — forget the old
        key's terminal state and re-enrol from scratch.

        Picks up the rotated key on the REST client (the client was
        built with the old key) and clears the stored validate-key
        snapshot, which belongs to the old key.
        """
        if hasattr(self._rest, "api_key"):
            self._rest.api_key = self._ctx.settings.server_token
        self._status.attempts = 0
        self._transient_failures = 0
        self._ctx.validate_key_result = None
        self._set_state(EnrolmentState.NOT_CONFIGURED)
        logger.info("[ENROL] new API key configured — re-enrolling from scratch")

    # ------------------------------------------------------------------
    def _register_state_waiter(self) -> asyncio.Event:
        waiter = asyncio.Event()
        self._state_waiters.add(waiter)
        return waiter

    async def _wait_state_change(self, stop: asyncio.Event) -> None:
        """Sleep until the state changes or ``stop`` is set (no polling)."""
        waiter = self._register_state_waiter()
        wait_task = asyncio.create_task(waiter.wait())
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wait_task, stop_task):
                task.cancel()
            await asyncio.gather(wait_task, stop_task, return_exceptions=True)
            self._state_waiters.discard(waiter)

    async def wait_until_registered(self, stop: asyncio.Event) -> bool:
        """Block until REGISTERED (True) or FAILED_AUTH / stop (False).

        Used by the WS channel gate (§5.1): the channel must not open
        until enrolment succeeded; while enrolment is still retrying
        this waits (woken by state events, not a 100 ms poll); on
        FAILED_AUTH the channel stays down.
        """
        while True:
            state = self._status.state
            if state == EnrolmentState.REGISTERED:
                return True
            if state == EnrolmentState.FAILED_AUTH:
                return False
            if stop.is_set():
                return False
            await self._wait_state_change(stop)

    async def run(self, stop: asyncio.Event) -> None:
        """Supervise enrolment until FAILED_AUTH or ``stop``.

        - Unconfigured → NOT_CONFIGURED, re-check every few seconds.
        - REGISTERED / VALIDATING → park until the state moves (a
          re-auth notification, a new key, or stop).
        - FAILED_TRANSIENT → back off (§5.4 schedule), then retry.
        - FAILED_AUTH → return (terminal for this key).
        """
        while not stop.is_set():
            if not self.is_configured:
                self._set_state(EnrolmentState.NOT_CONFIGURED)
                self._transient_failures = 0
                await self._wait_or_stop(stop, NOT_CONFIGURED_POLL_S)
                continue
            state = self._status.state
            if state in (EnrolmentState.REGISTERED, EnrolmentState.VALIDATING):
                # Park until the state moves (re-auth, new key, stop) —
                # woken by state events, no polling.
                while (
                    self._status.state in (EnrolmentState.REGISTERED, EnrolmentState.VALIDATING) and not stop.is_set()
                ):
                    await self._wait_state_change(stop)
                continue
            if state == EnrolmentState.FAILED_AUTH:
                # Terminal for this key — fall through to the manifest
                # cleanup below instead of returning directly.
                break
            if state == EnrolmentState.FAILED_TRANSIENT:
                delay = self._next_backoff_delay()
                self.backoff_delays.append(delay)
                await self._wait_or_stop(stop, delay)
                if stop.is_set() or not self.is_configured:
                    continue
            await self.enrol_once()
        # Supervisor exiting (stop set, or FAILED_AUTH): a best-effort
        # manifest POST must not keep running past shutdown.
        await self._cancel_manifest_task()

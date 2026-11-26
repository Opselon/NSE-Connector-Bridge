"""Unit tests for device enrolment at startup (CONTRACT v1 §3.1/§3.2, §5.1).

Label: unit. No MT5, no network — httpx.MockTransport answers, and the
WS gate test patches ``websockets.connect`` so no socket is opened.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.enrolment import MAX_TRANSIENT_ATTEMPTS, EnrolmentManager, enrolment_backoff_delay
from nse_bridge.server.errors import ServerErrorCode
from nse_bridge.server.rest import RestClient

pytestmark = pytest.mark.unit


def _settings(**overrides) -> BridgeSettings:
    base = {
        "server_base_url": "http://127.0.0.1:1",
        "environment": "test",
        "server_token": "tok-SECRET",
        "device_id": "dev-1",
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


def _ctx(settings: BridgeSettings) -> BridgeContext:
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


def _record_sleep(sleeps: list[float]):
    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    return _sleep


def _manager(handler, manager_kwargs=None, **overrides):
    """EnrolmentManager + RestClient over a MockTransport.

    Returns ``(manager, rest, sleeps, ctx)``; the caller closes ``rest``.
    """
    settings = _settings(**{k: v for k, v in overrides.items() if k in ("server_token", "server_base_url")})
    sleeps: list[float] = []
    rest = RestClient(
        settings,
        "dev-1",
        transport=httpx.MockTransport(handler),
        sleep=_record_sleep(sleeps),
    )
    ctx = _ctx(settings)
    manager = EnrolmentManager(
        ctx,
        rest,
        retry_base_s=0.05,
        jitter=False,
        **(manager_kwargs or {}),
    )
    return manager, rest, sleeps, ctx


def _ok_validate(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "valid": True,
            "user_id": "user-1",
            "subscription": {"active": True, "plan": "pro"},
            "entitlements": {
                "history_sync": True,
                "ai_coach": False,
                "position_manager": True,
                "auto_trade": True,
                "max_devices": 3,
                "max_accounts": 2,
            },
        },
    )


def _ok_register(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "registered": True,
            "session_expires_at": "2099-01-01T00:00:00Z",
        },
    )


@pytest.mark.unit
def test_enrolment_backoff_doubles_from_500ms_and_caps_at_30s():
    assert enrolment_backoff_delay(0) == pytest.approx(0.5)
    assert enrolment_backoff_delay(1) == pytest.approx(1.0)
    assert enrolment_backoff_delay(2) == pytest.approx(2.0)
    assert enrolment_backoff_delay(6) == pytest.approx(30.0)  # capped
    assert enrolment_backoff_delay(20) == pytest.approx(30.0)


@pytest.mark.unit
async def test_enrolment_success_stores_subscription_and_session():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("manifest"):
            return httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})
        return _ok_register(request)

    manager, rest, _sleeps, _ctx = _manager(handler)
    assert await manager.enrol_once() is True
    # The manifest POST is a background task now (see enrol_once): wait
    # for it before asserting on the recorded request paths.
    await manager.wait_for_manifest()
    assert manager.state == "registered"
    assert manager.is_registered
    assert calls == [
        "/api/v1/connector/validate-key",
        "/api/v1/connector/register-device",
        "/api/v1/connector/manifest",  # §4.1: manifest posted once enrolled
    ]
    # Server-resolved subscription/entitlements land on the context.
    stored = manager._ctx.validate_key_result
    assert stored is not None
    assert stored["valid"] is True
    assert stored["user_id"] == "user-1"  # server-resolved, never client-asserted
    assert stored["subscription"] == {"active": True, "plan": "pro"}
    assert stored["entitlements"]["position_manager"] is True
    assert stored["entitlements"]["max_devices"] == 3
    # Session expiry is exposed for the GUI + diagnostics.
    assert manager.session_expires_at == "2099-01-01T00:00:00Z"
    status = manager.to_dict()
    assert status["state"] == "registered"
    assert status["session_expires_at"] == "2099-01-01T00:00:00Z"
    assert status["user_id"] == "user-1"
    assert status["last_error"] is None
    await rest.close()


@pytest.mark.unit
async def test_enrolment_401_on_validate_key_is_failed_auth_no_retry():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "bad key"}})

    manager, rest, _sleeps, _ctx = _manager(handler)
    assert await manager.enrol_once() is False
    assert manager.state == "failed_auth"
    assert manager.to_dict()["last_error"] == "key_invalid"
    assert calls == ["/api/v1/connector/validate-key"]  # never retried with the same key (§3.1)
    # Terminal: the supervisor exits instead of retrying.
    stop = asyncio.Event()
    await manager.run(stop)
    assert manager.state == "failed_auth"
    await rest.close()


@pytest.mark.unit
async def test_enrolment_403_on_register_device_max_devices_is_failed_auth():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        return httpx.Response(403, json={"error": {"code": "device_limit_exceeded", "message": "too many"}})

    manager, rest, _sleeps, _ctx = _manager(handler)
    assert await manager.enrol_once() is False
    assert manager.state == "failed_auth"
    assert manager.to_dict()["last_error"] == "device_limit_exceeded"
    # No eviction attempt: exactly one validate + one register, nothing else.
    assert calls == [
        "/api/v1/connector/validate-key",
        "/api/v1/connector/register-device",
    ]
    await rest.close()


@pytest.mark.unit
async def test_enrolment_500_then_success_retries_with_backoff():
    # The REST client retries 5xx internally (initial + 3); the
    # manager only sees FAILED_TRANSIENT once those are exhausted.
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # First 4 validate-key attempts fail (one enrol_once worth of
        # internal retries), the 5th succeeds.
        if request.url.path.endswith("validate-key") and calls.count(request.url.path) <= 4:
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        return _ok_register(request)

    manager, rest, sleeps, _ctx = _manager(handler)
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        deadline = asyncio.get_event_loop().time() + 5.0
        while manager.state != "registered":
            assert asyncio.get_event_loop().time() < deadline, f"stuck in {manager.state}"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    assert manager.state == "registered"
    assert manager.to_dict()["last_error"] is None
    # FAILED_TRANSIENT first, then REGISTERED; one backoff step recorded.
    assert manager.backoff_delays == [pytest.approx(0.05)]
    assert calls.count("/api/v1/connector/validate-key") == 5  # 4 failed + 1 good
    # REST internal 5xx backoff: doubling within the ±15% jitter band
    assert [0.5 * 2**i * 0.85 <= s <= 0.5 * 2**i * 1.15 for i, s in enumerate(sleeps)] == [True, True, True]
    await rest.close()


@pytest.mark.unit
async def test_enrolment_not_configured_without_key():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"valid": True})

    manager, rest, _sleeps, _ctx = _manager(handler, server_token="")
    assert manager.is_configured is False
    assert await manager.enrol_once() is False
    assert manager.state == "not_configured"
    assert calls == []  # nothing is ever sent without a key
    # The supervisor parks (no calls, no crash) until stopped.
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    await asyncio.sleep(0.3)
    assert manager.state == "not_configured"
    assert calls == []
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)
    await rest.close()


@pytest.mark.unit
async def test_reenrolment_after_401_on_sync_batch():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("register-device"):
            return _ok_register(request)
        if request.url.path.endswith("sync/batch") and calls.count(request.url.path) == 1:
            return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "stale session"}})
        return httpx.Response(200, json={"accepted": True, "batch_id": "b1"})

    manager, rest, _sleeps, _ctx = _manager(handler)
    rest.enrolment = manager
    assert await manager.enrol_once() is True
    ack = await rest.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert ack.accepted is True
    # Re-enrolment happened once (register-device), then the batch retried.
    assert calls.count("/api/v1/connector/register-device") == 2  # startup + re-enrolment
    assert calls.count("/api/v1/connector/sync/batch") == 2
    assert manager.state == "registered"
    assert manager.session_expires_at == "2099-01-01T00:00:00Z"
    await rest.close()


@pytest.mark.unit
async def test_reenrolment_failure_with_revoked_key_is_failed_auth():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("register-device"):
            # Startup enrolment succeeds; the later re-enrolment 401s.
            if calls.count(request.url.path) == 1:
                return _ok_register(request)
            return httpx.Response(401, json={"error": {"code": "key_revoked", "message": "revoked"}})
        return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "stale"}})

    manager, rest, _sleeps, _ctx = _manager(handler)
    rest.enrolment = manager
    assert await manager.enrol_once() is True
    with pytest.raises(Exception) as excinfo:
        await rest.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert getattr(excinfo.value, "code", None) is ServerErrorCode.KEY_REVOKED
    assert manager.state == "failed_auth"
    assert manager.to_dict()["last_error"] == "key_revoked"
    await rest.close()


@pytest.mark.unit
async def test_reset_for_new_key_clears_failed_auth_and_reenrols():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            # First key is bad; the rotated key is good.
            if rest.api_key == "tok-SECRET":
                return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "bad key"}})
            return _ok_validate(request)
        return _ok_register(request)

    manager, rest, _sleeps, _ctx = _manager(handler)
    assert await manager.enrol_once() is False
    assert manager.state == "failed_auth"
    # Rotate the key (as PUT /settings does) and reset the manager.
    manager._ctx.settings.server_token = "tok-NEW"
    manager.reset_for_new_key()
    assert rest.api_key == "tok-NEW"  # the client picks up the rotated key
    assert manager._ctx.validate_key_result is None
    assert await manager.enrol_once() is True
    assert manager.state == "registered"
    await rest.close()


# ---------------------------------------------------------------------------
# B2: terminal FAILED after N transient failures + slow-schedule retry
# ---------------------------------------------------------------------------
def _always_503(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})


@pytest.mark.unit
async def test_enrolment_terminal_failed_after_10_transient_failures():
    """MAX_TRANSIENT_ATTEMPTS = 10: the 10th consecutive transient
    failure lands in FAILED (terminal for the fast-retry cycle) with
    the exact server reason kept verbatim and a scheduled slow retry —
    never a permanent "validating" and never unbounded fast retries."""
    assert MAX_TRANSIENT_ATTEMPTS == 10
    manager, rest, _sleeps, _ctx = _manager(
        _always_503,
        # Slow retry is long here on purpose: the supervisor must still
        # be sitting in FAILED (not already on its next cycle) while
        # the assertions below read the terminal state.
        manager_kwargs={"retry_cap_s": 0.05, "slow_retry_s": 30.0},
    )
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while manager.state != "failed":
            assert asyncio.get_event_loop().time() < deadline, f"stuck in {manager.state}"
            await asyncio.sleep(0.05)
        assert manager.to_dict()["attempts"] == 10
        assert manager.to_dict()["transient_failures"] == 10
        assert manager.to_dict()["terminal"] is True
        assert manager.to_dict()["last_error"] == "server_unavailable"
        assert manager.to_dict()["last_error_message"] == "down"
        assert manager.to_dict()["next_retry_at"] is not None
        # Fast backoff ran for the first 9 failures only (the 10th
        # goes terminal instead of scheduling another fast retry).
        assert len(manager.backoff_delays) == 9
        # The WS channel gate refuses a terminal enrolment.
        assert await manager.wait_until_registered(stop) is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    await rest.close()


@pytest.mark.unit
async def test_enrolment_failed_slow_retry_recovers():
    """After FAILED the supervisor keeps retrying on the slow schedule
    (FAILED_SLOW_RETRY_S) — a recovered server re-registers the device
    instead of needing a restart or a new key."""
    mode = {"ok": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not mode["ok"]:
            return _always_503(request)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("manifest"):
            return httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})
        return _ok_register(request)

    manager, rest, _sleeps, _ctx = _manager(
        handler,
        manager_kwargs={"retry_cap_s": 0.05, "slow_retry_s": 0.2},
    )
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while manager.state != "failed":
            assert asyncio.get_event_loop().time() < deadline, f"stuck in {manager.state}"
            await asyncio.sleep(0.05)
        mode["ok"] = True  # the server recovers
        deadline = asyncio.get_event_loop().time() + 10.0
        while manager.state != "registered":
            assert asyncio.get_event_loop().time() < deadline, f"stuck in {manager.state}"
            await asyncio.sleep(0.05)
        assert manager.to_dict()["terminal"] is False
        assert manager.to_dict()["next_retry_at"] is None
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    await rest.close()


@pytest.mark.unit
async def test_stale_validating_watchdog_drives_enrolment():
    """The 2026-11-26 incident: a re-auth whose completion callback
    never fires left the manager parked in VALIDATING forever. The
    watchdog drives an enrolment attempt once VALIDATING outlives
    VALIDATING_STALE_S, so the state always resolves."""
    manager, rest, _sleeps, _ctx = _manager(
        lambda request: (
            _ok_validate(request)
            if request.url.path.endswith("validate-key")
            else (
                httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})
                if request.url.path.endswith("manifest")
                else _ok_register(request)
            )
        ),
        manager_kwargs={"validating_stale_s": 0.3},
    )
    assert await manager.enrol_once() is True
    assert manager.state == "registered"
    # A §3.2 re-auth starts (VALIDATING) and its completion never
    # lands — the supervisor must not park there forever.
    manager.note_reauth_start()
    assert manager.state == "validating"
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        deadline = asyncio.get_event_loop().time() + 5.0
        while manager.state != "registered":
            assert asyncio.get_event_loop().time() < deadline, f"stuck in {manager.state}"
            await asyncio.sleep(0.05)
        assert manager.to_dict()["attempts"] == 2  # watchdog drove the second attempt
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    await rest.close()


@pytest.mark.unit
async def test_request_revalidation_reruns_enrolment():
    """request_revalidation() (WS-auth failures while REST validation
    succeeds) wakes the parked supervisor, which re-runs enrol_once —
    the recovery path for a stale device session."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("manifest"):
            return httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})
        return _ok_register(request)

    manager, rest, _sleeps, _ctx = _manager(handler)
    assert await manager.enrol_once() is True
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    try:
        await asyncio.sleep(0.2)  # supervisor parked in REGISTERED
        assert manager.request_revalidation() is True
        deadline = asyncio.get_event_loop().time() + 5.0
        while calls.count("/api/v1/connector/validate-key") < 2:
            assert asyncio.get_event_loop().time() < deadline, "revalidation never ran"
            await asyncio.sleep(0.05)
        assert manager.state == "registered"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    # Not honoured outside REGISTERED (the supervisor owns retries):
    # a fresh manager that never registered refuses the request.
    manager2, rest2, _sleeps2, _ctx2 = _manager(handler)
    manager2._set_state("failed_transient")
    assert manager2.request_revalidation() is False
    await rest2.close()
    await rest.close()


# ---------------------------------------------------------------------------
# WS channel gate (§5.1)
# ---------------------------------------------------------------------------
class _BlockingEnrolment:
    """Enrolment stub that stays VALIDATING until released."""

    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.state = "validating"

    async def wait_until_registered(self, stop: asyncio.Event) -> bool:
        while not self.released.is_set():
            if stop.is_set():
                return False
            await asyncio.sleep(0.05)
        return True

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "last_error": None}


class _FailingEnrolment:
    """Enrolment stub that is terminally FAILED_AUTH."""

    async def wait_until_registered(self, stop: asyncio.Event) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {"state": "failed_auth", "last_error": "key_invalid"}


class _FakeWS:
    """websockets.connect replacement: records the attempt, then fails."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def __aenter__(self):
        raise ConnectionRefusedError("mock ws down")

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.unit
async def test_channel_does_not_open_ws_before_registered(monkeypatch):
    connect_calls: list[str] = []

    def fake_connect(url: str, **kwargs):
        connect_calls.append(url)
        return _FakeWS(connect_calls)

    monkeypatch.setattr("websockets.connect", fake_connect)
    settings = _settings()
    ctx = _ctx(settings)
    channel = ServerChannel(
        ctx,
        EventBus(),
        enrolment=_BlockingEnrolment(),
    )
    channel.start()
    try:
        await asyncio.sleep(0.3)
        assert connect_calls == []  # gate holds while enrolment is still retrying
        assert channel.running
        # Release enrolment → the channel may now connect (and fail:
        # the fake socket refuses — the attempt itself is the proof).
        channel._enrolment.released.set()
        deadline = asyncio.get_event_loop().time() + 5.0
        while not connect_calls:
            assert asyncio.get_event_loop().time() < deadline, "channel never connected after enrolment"
            await asyncio.sleep(0.05)
        assert connect_calls[0] == settings.resolved_server_ws_url
    finally:
        await channel.stop()


@pytest.mark.unit
async def test_channel_stays_down_on_failed_auth(monkeypatch):
    connect_calls: list[str] = []

    def fake_connect(url: str, **kwargs):
        connect_calls.append(url)
        return _FakeWS(connect_calls)

    monkeypatch.setattr("websockets.connect", fake_connect)
    settings = _settings()
    ctx = _ctx(settings)
    channel = ServerChannel(
        ctx,
        EventBus(),
        enrolment=_FailingEnrolment(),
    )
    channel.start()
    try:
        await asyncio.sleep(0.3)
        assert connect_calls == []  # FAILED_AUTH: no WS attempt, ever
        assert not channel.running  # the supervisor exited with the reason exposed
        assert "failed_auth" in (ctx.server_channel_state.get("last_error") or "")
        assert "key_invalid" in (ctx.server_channel_state.get("last_error") or "")
    finally:
        await channel.stop()

"""Unit tests for the CONTRACT v1 REST client (contract §3).

Label: unit. No MT5, no network — httpx.MockTransport answers.
"""

from __future__ import annotations

import httpx
import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.server.errors import ServerError, ServerErrorCode
from nse_bridge.server.rest import RestClient

pytestmark = pytest.mark.unit


def _settings(**overrides) -> BridgeSettings:
    base = {
        "server_base_url": "http://127.0.0.1:1",
        "environment": "test",
        "server_token": "tok-SECRET",
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


def _client(handler, **overrides):
    sleeps: list[float] = []
    client = RestClient(
        _settings(**overrides),
        "dev-1",
        transport=httpx.MockTransport(handler),
        sleep=_record_sleep(sleeps),
    )
    client._sleeps = sleeps  # type: ignore[attr-defined]
    return client


def _record_sleep(sleeps: list[float]):
    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    return _sleep


def _ok_validate(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer tok-SECRET"
    assert request.headers["X-NSE-Device-Id"] == "dev-1"
    return httpx.Response(200, json={
        "valid": True,
        "user_id": "user-1",
        "subscription": {"active": True, "plan": "pro"},
        "entitlements": {
            "history_sync": True, "ai_coach": False, "position_manager": True,
            "auto_trade": True, "max_devices": 3, "max_accounts": 2,
        },
    })


@pytest.mark.unit
async def test_validate_key_success_stores_server_resolved_state():
    client = _client(_ok_validate)
    result = await client.validate_key()
    assert result.valid is True
    assert result.user_id == "user-1"  # server-resolved, never client-asserted
    assert result.entitlements.position_manager is True
    assert result.entitlements.ai_coach is False
    assert result.entitlements.max_devices == 3
    assert client.user_id == "user-1"
    await client.close()


@pytest.mark.unit
async def test_validate_key_401_key_invalid_no_retry():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "bad key"}})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.validate_key()
    assert excinfo.value.code is ServerErrorCode.KEY_INVALID
    assert excinfo.value.is_fatal_auth
    assert len(calls) == 1  # never retried with the same key (§3.1)
    await client.close()


@pytest.mark.unit
async def test_validate_key_401_unknown_code_is_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "something_new", "message": "?"}})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.validate_key()
    assert excinfo.value.code is ServerErrorCode.MALFORMED_RESPONSE
    await client.close()


@pytest.mark.unit
async def test_validate_key_403_device_limit_mapped_distinctly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "device_limit_exceeded", "message": "too many"}})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.validate_key()
    assert excinfo.value.code is ServerErrorCode.DEVICE_LIMIT_EXCEEDED
    await client.close()


@pytest.mark.unit
async def test_validate_key_valid_false_is_not_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"valid": False})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.validate_key()
    assert excinfo.value.code is ServerErrorCode.KEY_INVALID
    await client.close()


@pytest.mark.unit
async def test_register_device_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/connector/register-device"
        return httpx.Response(200, json={
            "registered": True, "session_expires_at": "2099-01-01T00:00:00Z",
        })

    client = _client(handler)
    result = await client.register_device()
    assert result.registered is True
    assert result.session_expires_at == "2099-01-01T00:00:00Z"
    assert client.session_expires_at == "2099-01-01T00:00:00Z"
    await client.close()


@pytest.mark.unit
async def test_send_batch_429_then_success_backs_off():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}})
        return httpx.Response(200, json={"accepted": True, "batch_id": "b1"})

    client = _client(handler)
    ack = await client.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert ack.accepted is True and ack.batch_id == "b1"
    assert len(calls) == 2
    assert client._sleeps == [0.5]  # retry_base_s, first backoff step
    await client.close()


@pytest.mark.unit
async def test_send_batch_5xx_retried_then_gives_up():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})

    client = _client(handler, )
    with pytest.raises(ServerError) as excinfo:
        await client.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert excinfo.value.code is ServerErrorCode.SERVER_UNAVAILABLE
    assert excinfo.value.retryable is True
    assert len(calls) == 4  # initial + 3 retries
    assert client._sleeps == [0.5, 1.0, 2.0]  # doubling backoff
    await client.close()


@pytest.mark.unit
async def test_send_batch_401_reenrols_once_then_retries():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/connector/sync/batch" and calls.count(request.url.path) == 1:
            return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "stale session"}})
        if request.url.path == "/api/v1/connector/register-device":
            return httpx.Response(200, json={"registered": True, "session_expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(200, json={"accepted": True, "batch_id": "b1"})

    client = _client(handler)
    ack = await client.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert ack.accepted is True
    assert calls.count("/api/v1/connector/register-device") == 1
    assert calls.count("/api/v1/connector/sync/batch") == 2
    await client.close()


@pytest.mark.unit
async def test_send_batch_401_reenrol_fails_with_revoked_key():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/connector/register-device":
            return httpx.Response(401, json={"error": {"code": "key_revoked", "message": "revoked"}})
        return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "stale"}})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    assert excinfo.value.code is ServerErrorCode.KEY_REVOKED
    await client.close()


@pytest.mark.unit
async def test_send_batch_accepted_false_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "accepted": False, "batch_id": "b1",
            "error": {"code": "validation_failed", "message": "bad kind"},
        })

    client = _client(handler)
    with pytest.raises(ServerError):
        await client.send_batch({"batch_id": "b1", "kind": "account", "items": {}, "count": 1})
    await client.close()


@pytest.mark.unit
async def test_heartbeat_reports_pending_commands():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/connector/heartbeat"
        return httpx.Response(200, json={
            "ok": True, "server_time": "2026-11-18T00:00:00Z", "pending_commands": 3,
        })

    client = _client(handler)
    result = await client.heartbeat()
    assert result.ok is True
    assert result.pending_commands == 3
    assert result.server_time == "2026-11-18T00:00:00Z"
    await client.close()


@pytest.mark.unit
async def test_session_refresh_and_revoke():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("session/refresh"):
            return httpx.Response(200, json={
                "refreshed": True, "session_expires_at": "2099-06-01T00:00:00Z",
            })
        return httpx.Response(200, json={"revoked": True})

    client = _client(handler)
    refreshed = await client.session_refresh()
    assert refreshed.session_expires_at == "2099-06-01T00:00:00Z"
    assert await client.session_revoke() is True
    assert client.session_expires_at is None  # revoke clears the local session
    assert seen == ["/api/v1/connector/session/refresh", "/api/v1/connector/session/revoke"]
    await client.close()


@pytest.mark.unit
async def test_releases_latest_parsed_for_updater():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={
            "version": "0.2.0", "download_url": "https://example.invalid/x.exe",
            "sha256": "a" * 64, "size_bytes": 123, "min_connector_version": "0.1.0",
            "signature": "YmFzZTY0", "released_at": "2026-11-18T00:00:00Z",
        })

    client = _client(handler)
    release = await client.releases_latest()
    assert release.version == "0.2.0"
    assert release.sha256 == "a" * 64
    assert release.signature == "YmFzZTY0"
    await client.close()


@pytest.mark.unit
async def test_send_manifest_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"manifest_hash" in body
        return httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})

    client = _client(handler)
    ack = await client.send_manifest({"device_id": "dev-1", "manifest_hash": "deadbeef"})
    assert ack["accepted"] is True
    await client.close()


@pytest.mark.unit
async def test_error_messages_never_carry_key_material():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "key_invalid", "message": "bad key"}})

    client = _client(handler)
    with pytest.raises(ServerError) as excinfo:
        await client.validate_key()
    assert "tok-SECRET" not in str(excinfo.value)
    assert "tok-SECRET" not in repr(excinfo.value)
    await client.close()

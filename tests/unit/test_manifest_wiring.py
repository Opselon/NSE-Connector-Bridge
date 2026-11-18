"""Unit tests for the §4.1 manifest wiring (gap §5 B1 task 1).

Label: unit. No MT5, no network — httpx.MockTransport answers.

Contract §4.1 defines BOTH delivery paths, and this module tests both:
- REST ``POST /api/v1/connector/manifest`` on enrolment (unchanged-hash
  no-op — re-posting an identical manifest is a no-op ack server-side);
- the ``manifest.update`` MessageEnvelope (``event_type:
  "manifest.update"``) over WS when the manifest changes mid-session.
  No frame name is invented: ``manifest.update`` is the contract's own
  ``event_type``.
"""

from __future__ import annotations

import httpx
import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.enrolment import EnrolmentManager
from nse_bridge.server.errors import ServerError
from nse_bridge.server.manifest import (
    ManifestTracker,
    build_manifest,
    build_manifest_update_envelope,
    manifest_hash,
)
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


def _ok_manifest(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"accepted": True, "manifest_hash": "deadbeef"})


# ---------------------------------------------------------------------------
# Tracker + envelope
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_manifest_update_envelope_shape():
    manifest = build_manifest("dev-1")
    envelope = build_manifest_update_envelope(manifest, user_id="user-1")
    assert envelope["event_type"] == "manifest.update"  # contract §4.1, not invented
    assert envelope["protocol_version"] == "1.0"
    assert envelope["schema_version"] == "2026-11-17"
    assert envelope["message_id"]
    assert envelope["device_id"] == "dev-1"
    assert envelope["payload"] is manifest  # the §4.1 manifest itself
    assert envelope["user_id"] == "user-1"
    assert envelope["event_timestamp"].endswith("Z")


@pytest.mark.unit
def test_manifest_update_envelope_omits_unknown_user_id():
    envelope = build_manifest_update_envelope(build_manifest("dev-1"))
    assert "user_id" not in envelope  # omit if unknown (§6.5)


@pytest.mark.unit
async def test_tracker_unchanged_hash_is_noop():
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _ok_manifest(request)

    rest = RestClient(
        _settings(),
        "dev-1",
        transport=httpx.MockTransport(handler),
        sleep=_record_sleep([]),
    )
    tracker = ManifestTracker()
    manifest = build_manifest("dev-1")
    assert manifest["manifest_hash"] == manifest_hash()
    assert tracker.is_changed(manifest) is True  # nothing sent yet
    assert await tracker.send_via_rest(rest, manifest) is True
    assert tracker.last_sent_hash == manifest["manifest_hash"]
    assert posts == ["/api/v1/connector/manifest"]
    # Second send with the same hash: no POST at all.
    assert await tracker.send_via_rest(rest, manifest) is False
    assert posts == ["/api/v1/connector/manifest"]
    await rest.close()


@pytest.mark.unit
async def test_tracker_refused_manifest_is_not_marked_sent():
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={"accepted": False, "manifest_hash": "deadbeef"})

    rest = RestClient(
        _settings(),
        "dev-1",
        transport=httpx.MockTransport(handler),
        sleep=_record_sleep([]),
    )
    tracker = ManifestTracker()
    manifest = build_manifest("dev-1")
    with pytest.raises(ServerError):
        await tracker.send_via_rest(rest, manifest)
    assert tracker.last_sent_hash is None  # refused: NOT marked sent
    assert tracker.is_changed(manifest) is True  # so the next attempt retries
    await rest.close()


# ---------------------------------------------------------------------------
# Enrolment wiring: manifest posted on enrolment, once per hash
# ---------------------------------------------------------------------------
def _manager(handler, **overrides):
    settings = _settings(**overrides)
    rest = RestClient(
        settings,
        "dev-1",
        transport=httpx.MockTransport(handler),
        sleep=_record_sleep([]),
    )
    ctx = _ctx(settings)
    manager = EnrolmentManager(ctx, rest, retry_base_s=0.05, jitter=False)
    return manager, rest, ctx


@pytest.mark.unit
async def test_enrolment_posts_manifest_once_per_hash():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("manifest"):
            return _ok_manifest(request)
        return _ok_register(request)

    manager, rest, _ctx = _manager(handler)
    assert await manager.enrol_once() is True
    assert manager.state == "registered"
    assert calls.count("/api/v1/connector/manifest") == 1
    # A second enrolment (re-enrolment path) does NOT re-post the
    # unchanged manifest — the tracker remembers the hash.
    assert await manager.enrol_once() is True
    assert calls.count("/api/v1/connector/manifest") == 1
    assert calls.count("/api/v1/connector/validate-key") == 2
    assert calls.count("/api/v1/connector/register-device") == 2
    await rest.close()


@pytest.mark.unit
async def test_enrolment_stays_registered_when_manifest_post_fails():
    """A manifest failure must NOT un-enrol the device (§4.1 is
    [client-v1.1] best-effort on top of a successful enrolment)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("validate-key"):
            return _ok_validate(request)
        if request.url.path.endswith("manifest"):
            return httpx.Response(200, json={"accepted": False, "manifest_hash": "x"})
        return _ok_register(request)

    manager, rest, _ctx = _manager(handler)
    assert await manager.enrol_once() is True
    assert manager.state == "registered"
    assert manager.is_registered
    # The refused manifest was NOT marked sent, so a later enrolment
    # retries it (once the server accepts, it stops).
    assert manager.manifest_tracker.last_sent_hash is None
    await rest.close()

"""Unit tests for REST request cancellation (spec §2, gap §5 B1 task 3).

Label: unit. No MT5, no network — httpx.MockTransport answers, with an
async handler that sleeps so a request stays in flight long enough to
cancel.

Cancellation is ``asyncio`` task cancellation: ``RestClient.cancel_all()``
cancels every in-flight request, ``close()`` cancels them on shutdown,
and a cancelled request raises ``asyncio.CancelledError`` promptly —
never a ``ServerError``, never a retry, never the full 30 s timeout.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nse_bridge.config import BridgeSettings
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


def _slow_client(delay_s: float = 30.0):
    """RestClient whose validate-key handler sleeps ``delay_s``."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        await asyncio.sleep(delay_s)
        return httpx.Response(200, json={"valid": True, "user_id": "user-1"})

    client = RestClient(_settings(), "dev-1", transport=httpx.MockTransport(handler))
    return client, calls


@pytest.mark.unit
async def test_cancel_all_aborts_inflight_request_promptly():
    client, calls = _slow_client()
    task = asyncio.create_task(client.validate_key())
    await asyncio.sleep(0.2)
    assert client.inflight_count == 1
    assert calls == ["/api/v1/connector/validate-key"]
    cancelled = await asyncio.wait_for(client.cancel_all(), timeout=2.0)
    assert cancelled == 1
    # The request raises CancelledError promptly — not after 30 s.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert client.inflight_count == 0
    assert len(calls) == 1  # cancelled, never retried
    await client.close()


@pytest.mark.unit
async def test_cancel_all_with_nothing_inflight_is_noop():
    client, _calls = _slow_client()
    assert await client.cancel_all() == 0
    await client.close()


@pytest.mark.unit
async def test_close_cancels_inflight_requests():
    """Shutdown aborts a request instead of awaiting its full timeout."""
    client, _calls = _slow_client()
    task = asyncio.create_task(client.validate_key())
    await asyncio.sleep(0.2)
    assert client.inflight_count == 1
    await asyncio.wait_for(client.close(), timeout=2.0)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert client.inflight_count == 0


@pytest.mark.unit
async def test_cancelled_request_does_not_swallow_later_requests():
    """After a cancellation the client stays usable for the next call."""
    client, _calls = _slow_client()

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"valid": True, "user_id": "user-1"})

    task = asyncio.create_task(client.validate_key())
    await asyncio.sleep(0.2)
    await client.cancel_all()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Swap in a fast transport: the client still works.
    client._transport = httpx.MockTransport(ok_handler)
    client._client = None
    result = await asyncio.wait_for(client.validate_key(), timeout=2.0)
    assert result.valid is True
    await client.close()

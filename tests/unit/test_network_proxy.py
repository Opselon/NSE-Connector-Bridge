"""Explicit-proxy support tests (settings.network_proxy).

Verified 2026-11-21 on the production deployment: the direct ISP
route to bridge.nexusverse.space hangs REST (WS upgrade reaches the
auth layer, REST times out), while a local SOCKS5 proxy answers in
~3 s — users on such networks MUST be able to route the Bridge's
Platform traffic (REST + WS) through an explicit proxy. The ambient
environment stays untrusted (trust_env=False, unchanged): the proxy
is a deliberate configuration, validated at boot and at settings
update time.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from nse_bridge.config import BridgeSettings, validate_network_proxy


class TestValidateNetworkProxy:
    def test_empty_is_direct(self):
        assert validate_network_proxy("") is None
        assert validate_network_proxy(None) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "url",
        [
            "socks5://127.0.0.1:10808",
            "socks5h://127.0.0.1:10808",
            "http://proxy.example.org:3128",
            "https://proxy.example.org",
        ],
    )
    def test_valid_schemes(self, url):
        assert validate_network_proxy(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://host:21",
            "no-scheme-127.0.0.1:10808",
            "socks5://",  # no host
            "://host:10808",
        ],
    )
    def test_invalid_urls_raise(self, url):
        with pytest.raises(ValueError):
            validate_network_proxy(url)


class TestRestClientUsesProxy:
    async def test_rest_client_builds_with_proxy_url(self, tmp_path):
        """The pooled httpx client is constructed with the configured
        proxy (verifiable via the client's proxy mount, no traffic)."""
        import httpx

        from nse_bridge.server.rest import RestClient

        settings = BridgeSettings(
            data_dir=tmp_path,
            server_base_url="https://bridge.example.org",
            network_proxy="http://127.0.0.1:3128",
        )
        client = RestClient(settings, "device-1")
        try:
            http = client._get_client()
            assert isinstance(http, httpx.AsyncClient)
            # httpx exposes the configured proxy on its transport mounts
            # (dict: URLPattern -> transport); a proxy build mounts an
            # AsyncHTTPTransport bound to the proxy URL pattern.
            patterns = [str(k) for k in http._mounts]
            assert patterns, "expected proxy mount patterns on the httpx client"
        finally:
            await client.close()

    async def test_rest_client_direct_when_unset(self, tmp_path):
        import httpx

        from nse_bridge.server.rest import RestClient

        settings = BridgeSettings(data_dir=tmp_path, server_base_url="https://bridge.example.org")
        client = RestClient(settings, "device-1")
        try:
            http = client._get_client()
            assert isinstance(http, httpx.AsyncClient)
        finally:
            await client.close()


class TestWsChannelProxy:
    async def test_channel_passes_proxy_to_websockets(self, tmp_path, monkeypatch):
        """The WS dial forwards the configured proxy URL — verified by
        intercepting websockets.connect, no socket is opened."""
        from nse_bridge.config import BridgeSettings
        from nse_bridge.engine.common import BridgeContext
        from nse_bridge.mt5.driver import Mt5Driver
        from nse_bridge.realtime.channel import ServerChannel

        settings = BridgeSettings(
            data_dir=tmp_path,
            server_url="ws://127.0.0.1:1/ws",  # unreachable by design
            server_token="tok",
            network_proxy="socks5://127.0.0.1:10808",
        )
        ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
        channel = ServerChannel(ctx, bus=None)

        captured: dict[str, object] = {}

        class _FakeConnect:
            def __init__(self, captured: dict[str, object]) -> None:
                self._captured = captured

            async def __aenter__(self):
                raise ConnectionError("test: no connection intended")

            async def __aexit__(self, *exc: object):
                return False

        def fake_connect(url, **kwargs):
            self_probe = _FakeConnect(captured)
            captured["url"] = url
            captured["proxy"] = kwargs.get("proxy")
            return self_probe

        monkeypatch.setattr("websockets.connect", fake_connect)
        channel.start()
        task = channel._task
        assert task is not None
        try:
            await asyncio_wait_for_dead(task)
        finally:
            await channel.stop()
        assert captured["proxy"] == "socks5://127.0.0.1:10808"
        assert captured["url"].startswith("ws://")


async def asyncio_wait_for_dead(task, timeout: float = 5.0):
    import asyncio

    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (ConnectionError, OSError, TimeoutError):
        pass  # expected: the dial never succeeds

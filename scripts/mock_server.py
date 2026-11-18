"""CONTRACT v1 mock NSE Platform server (REST + WS) — integration-test server.

Speaks the contract, not the Bridge's old invented protocol:

- REST (§3): ``validate-key``, ``register-device``, ``sync/batch``
  (the ONLY sync channel), ``heartbeat``, ``session/refresh``,
  ``session/revoke``, ``releases/latest``, ``manifest`` (§4.1).
- WS (§5/§6/§17.1): upgrade auth in headers (Bearer +
  ``X-NSE-Device-Id``), NO ``hello`` frame, 15 s heartbeat exchange,
  ``sync_request`` + a demo ``command`` envelope on connect,
  ``chart.request`` / ``chart.subscribe`` / ``chart.unsubscribe``
  answers. Records every ``action.result`` / ``chart.data`` /
  ``chart.tick`` frame it receives.

Usage (standalone)::

    python scripts/mock_server.py [--ws-port 9099] [--rest-port 9098] [--token test-token]

Programmatic use (integration tests)::

    from mock_server import MockServer
    server = MockServer(token="test-token")
    server.start()
    # server.rest_base_url -> "http://127.0.0.1:<port>"
    # server.ws_url -> "ws://127.0.0.1:<port>/api/v1/connector/ws"
    ...
    server.stop()

Auth behaviour (for tests): token ``"bad-token"`` answers 401
``key_invalid`` on every REST call; ``"revoked-token"`` answers 401
``key_revoked``. Any other token is accepted. A missing/invalid
Bearer on the WS upgrade closes the socket with 4401.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import websockets

DEFAULT_TOKEN = "test-token"
BATCH_KINDS = ("account", "positions", "history_orders", "history_positions")
CHART_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")
SERVER_USER_ID = "00000000-0000-0000-0000-000000000001"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class MockServerState:
    """Everything the mock observed — read by integration tests."""

    token: str = DEFAULT_TOKEN
    demo_account_id: str = "12345678901"
    send_demo_command: bool = True
    pending_commands: int = 0
    fail_next_batch: bool = False
    batches: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    chart_frames_received: list[dict[str, Any]] = field(default_factory=list)
    heartbeats_received: int = 0
    rest_heartbeats_received: int = 0  # POST /api/v1/connector/heartbeat (§3.4)
    ws_connections: int = 0
    commands_sent: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_batch(self, batch: dict[str, Any]) -> None:
        with self._lock:
            self.batches.append(batch)

    def record_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.results.append(result)

    def wait_for_result(self, correlation_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for result in self.results:
                    if result.get("correlation_id") == correlation_id:
                        return result
            time.sleep(0.05)
        return None


def _demo_command(state: MockServerState, device_id: str) -> dict[str, Any]:
    issued = datetime.now(UTC)
    return {
        "type": "command",
        "command_id": "01900000-0000-7000-8000-000000000001",
        "account_id": state.demo_account_id,
        "device_id": device_id,
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "01900000-0000-7000-8000-000000000001",
    }


# ---------------------------------------------------------------------------
# REST (§3)
# ---------------------------------------------------------------------------
def make_rest_handler(state: MockServerState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # keep test output clean
            pass

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send(status, {"error": {"code": code, "message": message}})

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _auth(self) -> str | None:
            """Bearer token or None when auth fails. ``bad-token`` /
            ``revoked-token`` are the test hooks for §3.6 errors."""
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            token = auth[len("Bearer "):].strip()
            if token in ("bad-token", "revoked-token"):
                return token
            if token != state.token:
                return None
            if not self.headers.get("X-NSE-Device-Id"):
                return None
            return token

        def _require_auth(self) -> bool:
            token = self._auth()
            if token is None:
                self._error(401, "key_invalid", "missing or invalid API key")
                return False
            if token == "bad-token":
                self._error(401, "key_invalid", "unknown / malformed / expired API key")
                return False
            if token == "revoked-token":
                self._error(401, "key_revoked", "key or device revoked")
                return False
            return True

        def do_POST(self) -> None:
            if not self._require_auth():
                return
            body = self._read_json()
            path = self.path.split("?")[0]
            if path == "/api/v1/connector/validate-key":
                self._send(200, {
                    "valid": True,
                    "user_id": SERVER_USER_ID,
                    "subscription": {"active": True, "plan": "pro",
                                     "expires_at": "2099-01-01T00:00:00Z"},
                    "entitlements": {
                        "history_sync": True, "ai_coach": True,
                        "position_manager": True, "auto_trade": True,
                        "max_devices": 3, "max_accounts": 2,
                    },
                })
            elif path == "/api/v1/connector/register-device":
                self._send(200, {
                    "registered": True,
                    "session_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
                })
            elif path == "/api/v1/connector/sync/batch":
                if state.fail_next_batch:
                    state.fail_next_batch = False
                    self._send(200, {
                        "accepted": False,
                        "batch_id": body.get("batch_id"),
                        "error": {"code": "validation_failed", "message": "injected test failure"},
                    })
                    return
                if body.get("kind") not in BATCH_KINDS or not isinstance(body.get("items"), dict):
                    self._send(200, {
                        "accepted": False,
                        "batch_id": body.get("batch_id"),
                        "error": {"code": "validation_failed",
                                  "message": f"invalid batch kind {body.get('kind')!r}"},
                    })
                    return
                state.record_batch(body)
                self._send(200, {"accepted": True, "batch_id": body.get("batch_id")})
            elif path == "/api/v1/connector/heartbeat":
                with state._lock:
                    state.rest_heartbeats_received += 1
                self._send(200, {
                    "ok": True, "server_time": _now_iso(),
                    "pending_commands": state.pending_commands,
                })
            elif path == "/api/v1/connector/session/refresh":
                self._send(200, {
                    "refreshed": True,
                    "session_expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
                })
            elif path == "/api/v1/connector/session/revoke":
                self._send(200, {"revoked": True})
            elif path == "/api/v1/connector/manifest":
                state.manifests.append(body)
                self._send(200, {"accepted": True, "manifest_hash": body.get("manifest_hash")})
            else:
                self._error(404, "not_found", f"no such endpoint: {path}")

        def do_GET(self) -> None:
            if not self._require_auth():
                return
            path = self.path.split("?")[0]
            if path == "/api/v1/connector/releases/latest":
                self._send(200, {
                    "version": "0.2.0",
                    "download_url": "https://example.invalid/nse-bridge-0.2.0.exe",
                    "sha256": "0" * 64,
                    "size_bytes": 12345678,
                    "min_connector_version": "0.1.0",
                    "signature": None,
                    "released_at": "2026-11-18T00:00:00Z",
                })
            else:
                self._error(404, "not_found", f"no such endpoint: {path}")

    return Handler


# ---------------------------------------------------------------------------
# WS (§5/§6/§17.1)
# ---------------------------------------------------------------------------
def _canned_candles(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    base = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    return [
        {
            "time": (base + timedelta(minutes=15 * i)).isoformat().replace("+00:00", ""),
            "open": 1.10226 + i * 0.0001, "high": 1.10248 + i * 0.0001,
            "low": 1.10201 + i * 0.0001, "close": 1.10248 + i * 0.0001,
            "tick_volume": 438 + i, "spread": 1,
        }
        for i in range(2)
    ]


async def _ws_handler(ws: Any, state: MockServerState) -> None:
    headers = ws.request.headers
    auth = headers.get("Authorization", "")
    device_id = headers.get("X-NSE-Device-Id", "")
    if f"Bearer {state.token}" not in auth or not device_id:
        print("[MOCK] WS upgrade rejected — missing/invalid auth")
        await ws.close(code=4401, reason="unauthorized")
        return
    with state._lock:
        state.ws_connections += 1
    print(f"[MOCK] WS connected device={device_id}")

    async def send_demo() -> None:
        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"type": "sync_request"}))
        if state.send_demo_command:
            await asyncio.sleep(0.2)
            command = _demo_command(state, device_id)
            with state._lock:
                state.commands_sent.append(command)
            await ws.send(json.dumps(command))
            print(f"[MOCK] sent demo command {command['command_id']}")

    demo_task = asyncio.create_task(send_demo())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                print(f"[MOCK] non-JSON frame: {str(raw)[:120]}")
                continue
            # Client→server frames (§5.3): heartbeats and chart frames
            # carry ``type``; action.result carries ``event_type``
            # (§6.5 envelope). Accept either key.
            mtype = msg.get("type") or msg.get("event_type")
            if mtype == "heartbeat":
                with state._lock:
                    state.heartbeats_received += 1
                await ws.send(json.dumps({"type": "heartbeat"}))
            elif mtype == "action.result":
                state.record_result(msg)
                print(f"[MOCK] action.result correlation={msg.get('correlation_id')} status={msg.get('status')}")
            elif mtype in ("chart.data", "chart.tick"):
                with state._lock:
                    state.chart_frames_received.append(msg)
            elif mtype == "chart.request":
                await _answer_chart_request(ws, msg)
            elif mtype == "chart.subscribe":
                await _answer_chart_subscribe(ws, state, msg)
            elif mtype == "chart.unsubscribe":
                with state._lock:
                    state.subscriptions.pop(msg.get("subscription_id", ""), None)
            else:
                print(f"[MOCK] unexpected client frame type={mtype} (ignored)")
    except websockets.ConnectionClosed:
        pass
    finally:
        demo_task.cancel()
        print("[MOCK] WS disconnected")


async def _answer_chart_request(ws: Any, msg: dict[str, Any]) -> None:
    symbol = str(msg.get("symbol") or "")
    timeframe = str(msg.get("timeframe") or "")
    request_id = msg.get("request_id")
    count = msg.get("count", 100)
    if symbol == "UNKNOWN":
        await ws.send(json.dumps({
            "type": "chart.data", "request_id": request_id, "subscription_id": None,
            "symbol": symbol, "timeframe": timeframe, "candles": [],
            "broker_time": _now_iso(), "received_at": _now_iso(),
            "complete": False, "status": "error",
            "error_code": "symbol_not_found",
            "error_message": f"symbol {symbol!r} does not exist on this broker/terminal",
        }))
        return
    if timeframe not in CHART_TIMEFRAMES:
        await ws.send(json.dumps({
            "type": "chart.data", "request_id": request_id, "subscription_id": None,
            "symbol": symbol, "timeframe": timeframe, "candles": [],
            "broker_time": _now_iso(), "received_at": _now_iso(),
            "complete": False, "status": "error",
            "error_code": "unsupported_timeframe",
            "error_message": f"timeframe {timeframe!r} is outside M1..MN1",
        }))
        return
    candles = _canned_candles(symbol, timeframe)[: max(1, min(int(count), 1000))]
    await ws.send(json.dumps({
        "type": "chart.data", "request_id": request_id, "subscription_id": None,
        "symbol": symbol, "timeframe": timeframe, "candles": candles,
        "broker_time": candles[-1]["time"], "received_at": _now_iso(),
        "complete": True, "status": "ok",
    }))
    if msg.get("include_ticks"):
        await ws.send(json.dumps({
            "type": "chart.tick", "subscription_id": None, "request_id": request_id,
            "symbol": symbol,
            "ticks": [{"time_ms": "2026-11-16T19:53:00.034", "bid": 1.10042, "ask": 1.10043}],
            "broker_time": candles[-1]["time"], "received_at": _now_iso(),
        }))


async def _answer_chart_subscribe(ws: Any, state: MockServerState, msg: dict[str, Any]) -> None:
    subscription_id = msg.get("subscription_id")
    symbol = str(msg.get("symbol") or "")
    timeframe = str(msg.get("timeframe") or "")
    with state._lock:
        if len(state.subscriptions) >= 20:
            await ws.send(json.dumps({
                "type": "chart.data", "request_id": None, "subscription_id": subscription_id,
                "symbol": symbol, "timeframe": timeframe, "candles": [],
                "broker_time": _now_iso(), "received_at": _now_iso(),
                "complete": False, "status": "error",
                "error_code": "subscription_limit",
                "error_message": "more than 20 concurrent subscriptions on this device",
            }))
            return
        state.subscriptions[subscription_id] = msg
    candles = _canned_candles(symbol, timeframe)
    await ws.send(json.dumps({
        "type": "chart.data", "request_id": None, "subscription_id": subscription_id,
        "symbol": symbol, "timeframe": timeframe, "candles": candles,
        "broker_time": candles[-1]["time"], "received_at": _now_iso(),
        "complete": True, "status": "ok",
    }))


# ---------------------------------------------------------------------------
# Server harness (tests + CLI)
# ---------------------------------------------------------------------------
class MockServer:
    """In-process CONTRACT v1 mock server (REST on one port, WS on another)."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        rest_port: int = 0,
        ws_port: int = 0,
        token: str = DEFAULT_TOKEN,
        send_demo_command: bool = True,
    ) -> None:
        self.host = host
        self.state = MockServerState(token=token, send_demo_command=send_demo_command)
        self._rest_httpd = ThreadingHTTPServer(
            (host, rest_port), make_rest_handler(self.state)
        )
        self._rest_thread: threading.Thread | None = None
        self._ws_port = ws_port
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_server: Any = None

    @property
    def rest_port(self) -> int:
        return self._rest_httpd.server_address[1]

    @property
    def ws_port(self) -> int:
        return self._ws_port

    @property
    def rest_base_url(self) -> str:
        return f"http://{self.host}:{self.rest_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self._ws_port}/api/v1/connector/ws"

    def start(self) -> MockServer:
        self._rest_thread = threading.Thread(
            target=self._rest_httpd.serve_forever, name="mock-rest", daemon=True
        )
        self._rest_thread.start()

        async def _serve() -> None:
            self._ws_server = await websockets.serve(
                lambda ws: _ws_handler(ws, self.state), self.host, self._ws_port
            )
            self._ws_port = self._ws_server.sockets[0].getsockname()[1]

        self._ws_loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(
            target=self._run_ws_loop, name="mock-ws", daemon=True
        )
        self._ws_thread.start()
        fut = asyncio.run_coroutine_threadsafe(_serve(), self._ws_loop)
        fut.result(timeout=10)
        print(f"[MOCK] REST {self.rest_base_url}  WS {self.ws_url}")
        return self

    def _run_ws_loop(self) -> None:
        assert self._ws_loop is not None
        asyncio.set_event_loop(self._ws_loop)
        self._ws_loop.run_forever()

    def stop(self) -> None:
        self._rest_httpd.shutdown()
        self._rest_httpd.server_close()
        if self._ws_loop is not None and self._ws_server is not None:
            # websockets 17: Server.close() is a plain method (call it on
            # the loop thread); wait_closed() is the coroutine to await.
            self._ws_loop.call_soon_threadsafe(self._ws_server.close)
            fut = asyncio.run_coroutine_threadsafe(self._ws_server.wait_closed(), self._ws_loop)
            fut.result(timeout=5)
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5)


async def _amain(port: int, rest_port: int, token: str) -> None:
    server = MockServer(rest_port=rest_port, ws_port=port, token=token)
    server.start()
    print("[MOCK] mock NSE server running (token required) — Ctrl+C to stop")
    try:
        await asyncio.Future()  # run forever
    except asyncio.CancelledError:
        pass
    finally:
        server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="CONTRACT v1 mock NSE Platform server")
    parser.add_argument("--port", type=int, default=9099, help="WS port (default 9099)")
    parser.add_argument("--rest-port", type=int, default=9098, help="REST port (default 9098)")
    parser.add_argument("--token", type=str, default=DEFAULT_TOKEN)
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args.port, args.rest_port, args.token))
    except KeyboardInterrupt:
        print("[MOCK] stopped")


if __name__ == "__main__":
    main()

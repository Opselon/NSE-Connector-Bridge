"""FastAPI application factory.

Every registered command becomes one POST endpoint: POST /api/v1/{command}.
Swagger UI at /docs, OpenAPI schema at /openapi.json — this schema IS the
contract the NSE Platform server consumes to talk with this bridge.
"""

"""FastAPI application factory.

Every registered command becomes one POST endpoint: POST /api/v1/{command}.
Swagger UI at /docs, OpenAPI schema at /openapi.json — this schema IS the
contract the NSE Platform server consumes to talk with this bridge.

Real-time: /ws/ui (local dashboard) + outbound ServerChannel to the platform.

NOTE: no `from __future__ import annotations` here — dynamic endpoints need
eager annotation evaluation so FastAPI/pydantic can resolve `rm | None`.
"""

import asyncio
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Generic, TypeVar

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import BRIDGE_VERSION
from ..engine.common import BridgeContext
from ..engine.registry import REGISTRY, CommandSpec, all_commands
from ..errors import BridgeError, ErrorCode
from ..gui.dashboard import LOCAL_API_KEY_COOKIE, register_dashboard
from ..logging_setup import redact_text, sanitize_payload
from ..realtime.bus import EventBus, start_streaming, stop_streaming
from ..realtime.channel import ServerChannel
from ..secrets import ensure_local_api_key
from .management import router as management_router

T = TypeVar("T")

TAG_METADATA = [
    {"name": "system", "description": "Bridge identity & status (server-facing)"},
    {"name": "connection", "description": "Commands 1-9: MT5 connection & terminal"},
    {"name": "account", "description": "Commands 10-18: trading account"},
    {"name": "symbols", "description": "Commands 19-27: symbols & market specifications"},
    {"name": "market", "description": "Commands 28-36: real-time market data"},
    {"name": "charts", "description": "Commands 37-45: charts & market history"},
    {"name": "positions", "description": "Commands 46-54: open positions management"},
    {"name": "orders", "description": "Commands 55-63: pending orders & history"},
    {"name": "trading", "description": "Commands 64-72: trade execution (MUTATES account state)"},
    {"name": "risk", "description": "Commands 73-81: risk management & calculations"},
    {"name": "diagnostics", "description": "Commands 82-90: errors, logs, monitoring"},
]


class Envelope(BaseModel, Generic[T]):  # noqa: UP046 — pydantic/FastAPI generic compat; keep Generic[T]
    ok: bool = True
    data: T | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    retcode: int | None = None
    retcode_label: str | None = None
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    ok: bool = False
    error: ErrorDetail
    request_id: str | None = None


def _error_response(err: BridgeError, request_id: str) -> JSONResponse:
    payload = ErrorEnvelope(
        ok=False,
        error=ErrorDetail(
            code=err.code.value,
            message=redact_text(err.message),
            retcode=err.retcode,
            retcode_label=None,
            details=sanitize_payload(err.details) or None,
        ),
        request_id=request_id,
    )
    if err.retcode is not None:
        from ..retcodes import translate_retcode

        payload.error.retcode_label = translate_retcode(err.retcode)
    return JSONResponse(status_code=err.http_status, content=payload.model_dump())


def build_endpoint(spec: CommandSpec, ctx: BridgeContext):
    """Create the async endpoint closure for one command."""
    rm = spec.request_model

    if rm is not None:

        async def endpoint(request: Request, body: rm | None = None):  # type: ignore[valid-type]
            if body is None:
                body = rm()
            elif isinstance(body, dict):  # defensive: FastAPI passes validated models,
                body = rm(**body)  # but keep dict input safe too
            result = await run_in_threadpool(spec.handler, ctx, body)
            return {"ok": True, "data": result}

    else:

        async def endpoint(request: Request, body: dict | None = Body(None)):  # noqa: B008 — FastAPI Body() default is the documented pattern
            result = await run_in_threadpool(spec.handler, ctx, body)
            return {"ok": True, "data": result}

    endpoint.__name__ = spec.name
    return endpoint


def create_app(ctx: BridgeContext) -> FastAPI:
    # Mandatory local API key: auto-generated on first run and stored
    # in the secret store. There is no empty-key = open mode.
    ensure_local_api_key(ctx.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus = EventBus()
        app.state.bus = bus
        # CONTRACT v1 §3: REST client + sync engine (the ONLY sync
        # channel). The server channel reuses them; the pollers enqueue
        # account/positions batches into the engine.
        from ..server.rest import RestClient
        from ..server.sync import SyncEngine
        from ..updates import Updater

        rest_client = RestClient(ctx.settings, ctx.get_or_create_device_id())
        sync_engine = SyncEngine(rest_client, ctx)
        ctx.rest_client = rest_client
        ctx.sync_engine = sync_engine
        app.state.rest_client = rest_client
        app.state.sync_engine = sync_engine
        # Phase 5 verified updater (CONTRACT v1 §3.5): check/download/
        # stage only; install happens on restart via the staged script.
        app.state.updater = Updater(ctx.settings, rest_client_getter=lambda: ctx.rest_client)
        pollers, stop_event = start_streaming(ctx, bus)
        channel = ServerChannel(ctx, bus, rest_client=rest_client, sync_engine=sync_engine)
        channel.start()
        app.state.channel = channel
        ctx.bus = bus
        sync_engine.start_flush_loop(interval_s=5.0)  # engine tracks the task itself
        # every order dispatch outcome flows to dashboard + server as trade_event
        ctx.trade_metrics.event_sink = lambda evt: bus.publish("trade_event", evt)
        logger.info("[APP] realtime streaming started (server_channel={})", bool(ctx.settings.server_url))
        yield
        await channel.stop()
        await sync_engine.stop_flush_loop()
        await rest_client.close()
        # A verified staged update installs on restart: launch the
        # staged installer detached (best-effort; it only runs when a
        # verified update is staged AND the bridge runs frozen).
        updater = getattr(app.state, "updater", None)
        if updater is not None:
            try:
                updater.launch_restart_installer()
            except Exception:  # noqa: BLE001, S110 — shutdown hook must never break teardown
                pass
        stop_streaming(pollers, stop_event)
        ctx.trade_metrics.event_sink = None
        ctx.bus = None
        ctx.rest_client = None
        ctx.sync_engine = None
        logger.info("[APP] realtime streaming stopped")

    app = FastAPI(
        title="NSE MT5 Bridge API",
        version=BRIDGE_VERSION,
        description=(
            "**NSE Platform secure local bridge** — one endpoint per command. "
            "The bridge attaches to the client's local MetaTrader 5 terminal through the "
            "official native C++ IPC driver and exposes exactly the 90 NSE bridge commands "
            "documented below. All endpoints are uniform `POST` JSON calls returning "
            "`{ok, data}` envelopes; broker rejections carry the official MT5 retcode.\n\n"
            "*Security:* bind is localhost-only by default; a mandatory `X-API-Key` gate "
            "protects every /api/v1/ endpoint (auto-generated on first run, stored in the "
            "OS secret store); the platform talks to this bridge over an outbound-authenticated "
            "WebSocket channel (no inbound ports).\n\n"
            "*Real-time:* the local Control Center polls the local API; `/ws/ui` also streams ticks + account updates for local tooling."
        ),
        openapi_tags=TAG_METADATA,
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    # NOTE: the local AI Agent product (AgentService + agent routes) was
    # removed in the client-only refactor (Phase 3). The Bridge is
    # communication infrastructure; it never dispatches orders on its
    # own analysis. Local commands stay available; the server channel
    # enforces the contract §6.1 allowlist.
    app.include_router(management_router)
    register_dashboard(app)

    # --- middleware: request id + latency logs + mandatory API key ------
    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        t0 = time.perf_counter()
        path = request.url.path
        # Mandatory X-API-Key on EVERY local API request. No empty-key =
        # open mode; /health is a plain liveness probe, not an API call.
        if path.startswith("/api/v1/"):
            presented = request.headers.get("X-API-Key") or ""
            if not presented:
                # The loopback GUI (/ui, same app) carries the same key
                # in an HttpOnly + SameSite=Strict cookie set by the
                # server itself — page JS never sees the key, and the
                # credential is still mandatory on every call. No
                # unauthenticated exemption exists for the GUI.
                presented = request.cookies.get(LOCAL_API_KEY_COOKIE) or ""
            expected = ctx.settings.local_api_key or ""
            if not expected or not hmac.compare_digest(presented, expected):
                err = BridgeError(
                    ErrorCode.UNAUTHORIZED, "Missing or invalid X-API-Key header", http_status=401
                )
                return _error_response(err, request_id)
        try:
            response = await call_next(request)
        except BridgeError as exc:
            response = _error_response(exc, request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[API] unhandled error on {} {}", request.method, path)
            response = _error_response(
                BridgeError(ErrorCode.INTERNAL, f"Internal error: {redact_text(str(exc))}", http_status=500),
                request_id,
            )
        response.headers["X-Request-Id"] = request_id
        if path.startswith("/api/"):
            logger.info(
                "[API] {} {} -> {} ({:.1f}ms) rid={}",
                request.method, path, response.status_code,
                (time.perf_counter() - t0) * 1000.0, request_id,
            )
        return response

    # --- exception handlers ---------------------------------------------
    @app.exception_handler(BridgeError)
    async def bridge_error_handler(request: Request, exc: BridgeError):
        return _error_response(exc, getattr(request.state, "request_id", ""))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", "")
        return _error_response(
            BridgeError(
                ErrorCode.VALIDATION,
                "Request validation failed",
                details={"errors": exc.errors()[:20]},
                http_status=422,
            ),
            rid,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", "")
        return _error_response(
            BridgeError(ErrorCode.NOT_FOUND, str(exc.detail), http_status=exc.status_code),
            rid,
        )

    # --- system endpoints -------------------------------------------------
    @app.get("/health", tags=["system"], summary="Plain liveness probe (no auth)")
    async def health():
        return {"ok": True, "status": "alive", "mt5_connected": ctx.driver.connected}

    @app.get(
        "/api/v1/commands",
        tags=["system"],
        summary="List all bridge commands",
        description="The registry of all NSE bridge commands with categories and mutation flags.",
    )
    async def list_commands():
        return {
            "ok": True,
            "data": [
                {
                    "number": c.number,
                    "name": c.name,
                    "category": c.category,
                    "title": c.title,
                    "description": c.description,
                    "mutates": c.mutates,
                    "request_model": c.request_model.__name__ if c.request_model else None,
                    "response_model": c.response_model.__name__ if c.response_model else None,
                }
                for c in all_commands()
            ],
        }

    # --- local dashboard WebSocket -----------------------------------------
    @app.websocket("/ws/ui")
    async def ws_ui(websocket: WebSocket):
        """Live feed for the local GUI: ticks, account updates, status snapshots."""
        await websocket.accept()
        bus: EventBus = app.state.bus
        queue = bus.subscribe()
        reader_task = None

        async def reader():
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "subscribe":
                    symbol = str(msg.get("symbol", "")).strip()
                    if symbol:
                        ctx.subscriptions.add(symbol, int(msg.get("interval_ms", 250)))
                elif mtype == "unsubscribe":
                    ctx.subscriptions.remove(str(msg.get("symbol", "")).strip())

        try:
            reader_task = asyncio.create_task(reader())
            # initial snapshot so the UI paints instantly
            state = ctx.driver.connection_state_dict()
            await websocket.send_json(
                {
                    "type": "status",
                    "data": {
                        "mt5_state": state["state"],
                        "mt5_connected": ctx.driver.connected,
                        "bridge_version": BRIDGE_VERSION,
                        "device_id": ctx.get_or_create_device_id(),
                        "server_channel": ctx.server_channel_state,
                    },
                }
            )
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)
            if reader_task:
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 — dashboard socket teardown must never raise
                    pass

    # --- dynamic per-command endpoints ------------------------------------
    for spec in sorted(REGISTRY, key=lambda c: c.number):
        rm = spec.response_model
        response_model = Envelope[rm] if (rm and rm is not object) else None
        app.add_api_route(
            f"/api/v1/{spec.name}",
            build_endpoint(spec, ctx),
            methods=["POST"],
            operation_id=spec.name,
            summary=f"[{spec.number}] {spec.title}",
            description=spec.description,
            tags=[spec.category],
            response_model=response_model,
        )
        if spec.mutates:
            logger.debug("[REGISTRY] mutating command registered: {}", spec.name)

    return app

"""Shared engine helpers + app context."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeSettings
from ..errors import BridgeError, ErrorCode
from ..mt5.driver import Mt5Driver


class LogStore:
    """Ring buffer of bridge log records (get_bridge_logs)."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def sink(self, message: Any) -> None:
        rec = message.record
        try:
            with self._lock:
                self._items.append(
                    {
                        "ts": rec["time"].isoformat(),
                        "level": rec["level"].name,
                        "name": rec["name"],
                        "message": rec["message"],
                    }
                )
        except Exception:  # noqa: BLE001, S110 — a log sink must never raise
            pass

    def tail(self, limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if level:
            items = [i for i in items if i["level"] == level.upper()]
        return items[-limit:]


@dataclass
class TradeExecMetrics:
    """Order-dispatch telemetry (get_trade_execution_metrics)."""

    sent: int = 0
    done: int = 0
    rejected: int = 0
    unknown: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    retcodes: dict[str, int] = field(default_factory=dict)
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    event_sink: Any = None  # optional callable(dict) — publishes trade_event to the bus

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:  # noqa: BLE001, S110 — a metrics sink must never raise
                pass

    def record(self, op: str, ok: str, ms: float, retcode: int | None, detail: dict[str, Any] | None = None) -> None:
        event = {
            "ts": datetime_now_iso(),
            "op": op,
            "outcome": ok,
            "retcode": retcode,
            "duration_ms": round(ms, 2),
            **(detail or {}),
        }
        with self._lock:
            self.sent += 1
            self.total_ms += ms
            self.max_ms = max(self.max_ms, ms)
            if ok == "done":
                self.done += 1
            elif ok == "rejected":
                self.rejected += 1
            else:
                self.unknown += 1
            key = str(retcode)
            self.retcodes[key] = self.retcodes.get(key, 0) + 1
            self.recent.append(event)
        self._emit(event)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            avg = self.total_ms / self.sent if self.sent else 0.0
            return {
                "orders_sent": self.sent,
                "done": self.done,
                "rejected": self.rejected,
                "unknown": self.unknown,
                "avg_dispatch_ms": round(avg, 2),
                "max_dispatch_ms": round(self.max_ms, 2),
                "retcodes": dict(self.retcodes),
                "recent": list(self.recent),
            }


def datetime_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@dataclass
class Subscription:
    symbol: str
    interval_ms: int
    created_at: str
    last_tick: dict[str, Any] | None = None
    sent: int = 0


class SubscriptionManager:
    """Market tick subscriptions feeding both WS channels (dashboard + server)."""

    def __init__(self) -> None:
        self._subs: dict[str, Subscription] = {}
        self._lock = threading.Lock()

    def add(self, symbol: str, interval_ms: int = 250) -> Subscription:
        with self._lock:
            sub = self._subs.get(symbol)
            if sub:
                sub.interval_ms = min(sub.interval_ms, interval_ms)
                return sub
            sub = Subscription(
                symbol=symbol,
                interval_ms=interval_ms,
                created_at=datetime_now_iso(),
            )
            self._subs[symbol] = sub
            return sub

    def remove(self, symbol: str) -> bool:
        with self._lock:
            return self._subs.pop(symbol, None) is not None

    def snapshot(self) -> list[Subscription]:
        with self._lock:
            return list(self._subs.values())

    def record_tick(self, symbol: str, payload: dict[str, Any]) -> None:
        with self._lock:
            sub = self._subs.get(symbol)
            if sub:
                sub.last_tick = payload
                sub.sent += 1


@dataclass
class BridgeContext:
    """Runtime context shared by REST API, WS channels and the GUI."""

    settings: BridgeSettings
    driver: Mt5Driver
    started_at: float = field(default_factory=time.time)
    device_id: str = ""
    logs: LogStore = field(default_factory=LogStore)
    subscriptions: SubscriptionManager = field(default_factory=SubscriptionManager)
    trade_metrics: TradeExecMetrics = field(default_factory=TradeExecMetrics)
    bus: Any = None  # EventBus, wired at app startup (loop-bound; set once)
    rest_client: Any = None  # RestClient (CONTRACT v1 §3), wired at app startup
    sync_engine: Any = None  # SyncEngine (CONTRACT v1 §3.3), wired at app startup
    enrolment: Any = None  # EnrolmentManager (CONTRACT v1 §3.1/§3.2), wired at app startup
    telemetry: Any = None  # TelemetryEmitter (ARCHITECTURE-CONTRACT §8), wired at app startup
    self_healing: Any = None  # SelfHealingRegistry (§8), wired at app startup
    #: Canonical-ID resolver (spec §4 / gap C2), lazily built on the
    #: first canonical ``BASE/QUOTE`` order symbol — never built for
    #: exact broker names, so existing callers pay nothing for it.
    symbol_resolver: Any = None  # SymbolResolver, built by get_symbol_resolver
    # Last validate-key snapshot (CONTRACT v1 §3.1), stored by the GUI
    # refresh action (POST /api/v1/account/validate-key) from the Phase 2
    # REST client. Server-resolved only (user_id, subscription,
    # entitlements) — NEVER key material. None = never validated.
    validate_key_result: dict[str, Any] | None = None
    server_channel_state: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "connected": False,
            "url": "",
            "last_connected_at": None,
            "reconnects": 0,
            "last_error": None,
        }
    )

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def enrolment_status(self) -> dict[str, Any]:
        """Current enrolment state + last error + session expiry, for
        the GUI Connection screen and the diagnostics summary (§3.1/§3.2).

        Delegates to the EnrolmentManager when wired (app startup);
        before that, reports honestly as not_configured.
        """
        manager = self.enrolment
        if manager is not None:
            return manager.to_dict()
        rest = self.rest_client
        return {
            "state": "not_configured",
            "configured": bool(self.settings.server_token),
            "last_error": None,
            "last_error_message": None,
            "attempts": 0,
            "session_expires_at": getattr(rest, "session_expires_at", None),
            "user_id": getattr(rest, "user_id", None),
        }

    def get_or_create_device_id(self) -> str:
        if self.device_id:
            return self.device_id
        if self.settings.device_id:
            self.device_id = self.settings.device_id
        else:
            self.device_id = f"nse-{uuid.uuid4().hex[:12]}"
            self.settings.device_id = self.device_id
            from ..config import persist_settings

            try:
                persist_settings(self.settings)
            except Exception:  # noqa: BLE001, S110 — device-id persistence is best-effort
                pass
        return self.device_id


def account_info_model(ctx: BridgeContext):
    """Fresh account snapshot; raises NOT_CONNECTED when offline."""
    from ..mt5 import build_account_model

    ctx.driver.ensure_connected()

    def job(mt5: Any):
        raw = mt5.account_info()
        if raw is None:
            raise BridgeError(ErrorCode.MT5_CALL_FAILED, "account_info() returned None")
        return raw

    return build_account_model(ctx.driver.run(job, op="account_info"))


def get_symbol_resolver(ctx: BridgeContext):
    """Lazily build (and cache) the canonical-ID resolver for this context.

    The inventory loads on first use only; the resolver caches the
    broker snapshot (TTL 300 s). Call :meth:`invalidate` on the cached
    resolver when the broker environment changes (B3
    ``broker_connection_change``) — a mapping built against the old
    broker must never carry over silently.
    """
    if ctx.symbol_resolver is None:
        from ..symbols.inventory import SymbolInventory
        from ..symbols.resolver import SymbolResolver

        ctx.symbol_resolver = SymbolResolver(SymbolInventory(ctx.driver))
    return ctx.symbol_resolver


def resolve_order_symbol(ctx: BridgeContext, symbol: str) -> str:
    """Resolve an order symbol to the exact broker symbol (spec §4, gap C2).

    Only inputs containing ``/`` take the resolution path — a broker
    symbol name never contains ``/``, so every existing exact-name
    caller passes through untouched (zero behaviour change). A canonical
    ``BASE/QUOTE`` input is resolved through the C1 resolver: an
    ambiguous or unknown canonical ID is a VALIDATION rejection, never
    a guess and never a verbatim fallback; an unreachable broker symbol
    list is likewise a rejection, never a silent pass-through.
    """
    symbol = (symbol or "").strip()
    if "/" not in symbol:
        return symbol
    from ..symbols.resolver import ResolutionStatus

    try:
        result = get_symbol_resolver(ctx).resolve(symbol)
    except Exception as exc:  # unresolvable == untradable here
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Cannot resolve canonical symbol {symbol!r}: broker symbol list unavailable ({exc}).",
            http_status=422,
            details={"symbol": symbol, "status": "not_found"},
        ) from exc
    if result.status is ResolutionStatus.RESOLVED and result.exact_symbol:
        return result.exact_symbol
    raise BridgeError(
        ErrorCode.VALIDATION,
        f"Cannot resolve canonical symbol {symbol!r}: {result.status.value} — {result.reason}",
        http_status=422,
        details={
            "symbol": symbol,
            "status": result.status.value,
            "candidates": [c.name for c in result.candidates],
        },
    )


def require_symbol_info(ctx: BridgeContext, symbol: str):
    """symbol_info(symbol) or SYMBOL_NOT_FOUND."""
    from ..mt5 import build_symbol_model

    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.symbol_info(symbol)

    raw = ctx.driver.run(job, op="symbol_info", context={"symbol": symbol})
    if raw is None:
        raise BridgeError(
            ErrorCode.SYMBOL_NOT_FOUND,
            f"Symbol '{symbol}' not found on this server (select it first via symbol APIs).",
            details={"symbol": symbol},
        )
    return build_symbol_model(raw, ctx.settings.broker_utc_offset_minutes)


def resolve_filling_mode(ctx: BridgeContext, symbol: str) -> int:
    """Dynamic filling resolution from the broker bitmask (ported adapter logic).

    filling_mode flags: 1=FOK, 2=IOC; fallback RETURN.
    Returns ORDER_FILLING_* constant used by order_send requests.
    """
    from ..mt5.types import f as _f

    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.symbol_info(symbol)

    raw = ctx.driver.run(job, op="symbol_info", context={"symbol": symbol})
    if raw is None:
        return 1  # ORDER_FILLING_IOC default
    modes = int(_f(raw, "filling_mode", 0) or 0)
    if modes & 2:
        return 1  # ORDER_FILLING_IOC
    if modes & 1:
        return 0  # ORDER_FILLING_FOK
    return 2  # ORDER_FILLING_RETURN

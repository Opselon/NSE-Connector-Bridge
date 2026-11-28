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


#: TTL for cached per-symbol filling modes (P1-4, close-path IPC
#: caching). A broker's filling mode changes only when its symbol
#: spec changes; 60 s bounds the staleness while collapsing the
#: ``symbol_info`` hop out of every close/modify after the first.
#: The pre-send ``order_check`` still runs fresh on the exact request
#: every time, so a stale mode can only cost a rejected attempt, never
#: a wrong fill — and a terminal reconnect invalidates immediately
#: (see :class:`TerminalValueCache`).
FILLING_MODE_CACHE_TTL_S = 60.0


class TerminalValueCache:
    """Per-context cache for terminal values stable across calls (P1-4).

    The close path re-fetched two such values on EVERY attempt:
    the MT5 action/type constants (identical for the loaded package)
    and the per-symbol filling mode (a broker symbol-spec bitmask).
    Both are cached here, on the :class:`BridgeContext`, so every
    caller of :func:`mt5_trade_constants` /
    :func:`resolve_filling_mode` shares them with no signature change.

    Invalidation (audit requirement):

    * terminal reconnect — the entry records the driver object and its
      ``connection_epoch`` (``Mt5Driver.connection_epoch``, bumped on
      every successful connect); a different driver or a newer epoch
      misses and re-fetches. Drivers without the attribute (unit-test
      fakes) report epoch 0 and never bump, so the cache simply lives
      as long as the context — tests build a fresh context per case.
    * symbol-spec change — filling modes expire after
      ``FILLING_MODE_CACHE_TTL_S``; constants have no TTL because the
      loaded package's constants cannot change without a reconnect.
    * explicit — :meth:`invalidate` drops everything; call it from any
      future broker-change hook (the B3 ``broker_connection_change``
      self-healing path has no live ``on_refresh_symbols`` wiring today,
      so the TTL is the operative invalidation for spec changes).

    Fallback defaults are NEVER cached: a ``symbol_info`` returning
    ``None`` (unknown symbol) or an unparseable bitmask resolves to a
    default that must be re-derived on the next call, not frozen in.
    Thread-safe: closes/modifies can arrive from the WS and REST paths
    concurrently, and the serialized worker is the only other
    synchronization point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._driver: Any = None
        self._epoch: int = 0
        self._constants: dict[str, int] | None = None
        self._filling: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _epoch_for(driver: Any) -> int:
        return int(getattr(driver, "connection_epoch", 0) or 0)

    def _epoch_matches(self, driver: Any) -> bool:
        return self._driver is driver and self._epoch == self._epoch_for(driver)

    def get_constants(self, driver: Any) -> dict[str, int] | None:
        """Cached trade constants, or None on a miss (copy — callers
        must never mutate the cached dict)."""
        with self._lock:
            if self._constants is None or not self._epoch_matches(driver):
                return None
            return dict(self._constants)

    def set_constants(self, driver: Any, consts: dict[str, int]) -> None:
        with self._lock:
            self._driver = driver
            self._epoch = self._epoch_for(driver)
            self._constants = dict(consts)

    def get_filling(self, driver: Any, symbol: str) -> int | None:
        """Cached filling mode for ``symbol``, or None on a miss/expiry."""
        with self._lock:
            if not self._epoch_matches(driver):
                return None
            entry = self._filling.get(symbol)
            if entry is None:
                return None
            mode, fetched_at = entry
            if time.monotonic() - fetched_at >= FILLING_MODE_CACHE_TTL_S:
                del self._filling[symbol]
                return None
            return mode

    def set_filling(self, driver: Any, symbol: str, mode: int) -> None:
        with self._lock:
            self._driver = driver
            self._epoch = self._epoch_for(driver)
            self._filling[symbol] = (mode, time.monotonic())

    def invalidate(self) -> None:
        """Drop every cached value (reconnect / broker-change hook)."""
        with self._lock:
            self._driver = None
            self._epoch = 0
            self._constants = None
            self._filling.clear()


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
    #: Durable policy-move store (audit P1-8), wired at app startup —
    #: executed/failed autonomous moves persist here and are served
    #: by GET /api/v1/policy/moves. None in bare/test contexts.
    policy_move_store: Any = None
    #: Canonical-ID resolver (spec §4 / gap C2), lazily built on the
    #: first canonical ``BASE/QUOTE`` order symbol — never built for
    #: exact broker names, so existing callers pay nothing for it.
    symbol_resolver: Any = None  # SymbolResolver, built by get_symbol_resolver
    #: Close-path IPC cache (P1-4): trade constants + per-symbol
    #: filling modes, shared by every engine caller on this context.
    #: See :class:`TerminalValueCache` for TTL / reconnect invalidation.
    ipc_cache: TerminalValueCache = field(default_factory=TerminalValueCache)
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
            "last_heartbeat_at": None,
            "last_server_heartbeat_at": None,
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


def _coerce_filling_bitmask(raw: Any) -> int | None:
    """Map a broker ``filling_mode`` value to the MT5 bitmask (1=FOK, 2=IOC).

    Ints pass through; known strings map by name (``"IOC"``/``"FOK"``/
    ``"RETURN"``, case-insensitive, with or without the
    ``ORDER_FILLING_`` prefix); numeric strings parse as ints. Returns
    None for unparseable garbage (the caller falls back to IOC) — a
    non-numeric string (R3) must never crash order placement.
    """
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip().upper()
        if text in ("IOC", "ORDER_FILLING_IOC"):
            return 2
        if text in ("FOK", "ORDER_FILLING_FOK"):
            return 1
        if text in ("RETURN", "ORDER_FILLING_RETURN"):
            return 0
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def resolve_filling_mode(ctx: BridgeContext, symbol: str) -> int:
    """Dynamic filling resolution from the broker bitmask (ported adapter logic).

    filling_mode flags: 1=FOK, 2=IOC; fallback RETURN.
    Returns ORDER_FILLING_* constant used by order_send requests.

    P1-4: the bitmask comes from the broker symbol spec, which does
    not change between closes — the resolved mode is cached per symbol
    on the context (TTL :data:`FILLING_MODE_CACHE_TTL_S`, invalidated
    on terminal reconnect). The fallback defaults (unknown symbol /
    unparseable bitmask) are never cached. ``order_check`` still runs
    fresh on the exact request every time, so a stale cached mode can
    only cost a rejected attempt, never a wrong fill.
    """
    from ..mt5.types import f as _f

    ctx.driver.ensure_connected()
    cached = ctx.ipc_cache.get_filling(ctx.driver, symbol)
    if cached is not None:
        return cached

    def job(mt5: Any):
        return mt5.symbol_info(symbol)

    raw = ctx.driver.run(job, op="symbol_info", context={"symbol": symbol})
    if raw is None:
        return 1  # ORDER_FILLING_IOC default
    modes = _coerce_filling_bitmask(_f(raw, "filling_mode", 0))
    if modes is None:
        return 1  # ORDER_FILLING_IOC default on garbage
    if modes & 2:
        resolved = 1  # ORDER_FILLING_IOC
    elif modes & 1:
        resolved = 0  # ORDER_FILLING_FOK
    else:
        resolved = 2  # ORDER_FILLING_RETURN
    ctx.ipc_cache.set_filling(ctx.driver, symbol, resolved)
    return resolved

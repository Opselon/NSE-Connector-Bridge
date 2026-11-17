"""Shared engine helpers + app context."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..config import BridgeSettings
from ..errors import BridgeError, ErrorCode, not_connected
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
        except Exception:
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
            except Exception:
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
    server_channel_state: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False, "connected": False, "url": "", "last_connected_at": None,
            "reconnects": 0, "last_error": None,
        }
    )

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

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
            except Exception:
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

"""Chart / symbol data protocol (CONTRACT v1 §17.1, WS channel).

Chart and tick data travel over the WS gateway (§5), NOT over REST
batches. There is no MCP push in the MT5 MCP server — live updates
are produced by Connector-side polling of the local MT5 actions at
the subscribed interval.

Frames (server → Connector):

- ``chart.request`` — one-shot candles: ``{type, request_id (uuidv7),
  symbol, timeframe, from?, to?, count, include_ticks}``. Timeframe ∈
  ``M1 M5 M15 M30 H1 H4 D1 W1 MN1``; ``count`` ≤ 1000 (larger ranges
  MUST be paged by the server with successive requests). ``from``/``to``
  are broker-time strings (same verbatim rule as §3.3); if both are
  absent the latest ``count`` candles are returned.
- ``chart.subscribe`` — ``{type, subscription_id (uuidv7), symbol,
  timeframe, poll_interval_s, include_ticks}``. Poll 5–3600 s
  (default 15). Max 20 concurrent subscriptions per device; the 21st
  is refused with ``chart.error`` code ``subscription_limit``.
  Subscriptions push ``chart.data`` DELTAS (new/changed candles only)
  plus ``chart.tick`` when ``include_ticks`` is true.
- ``chart.unsubscribe`` — ``{type, subscription_id}``. Unsubscribing
  an unknown id is a no-op ack, never an error.

Frames (Connector → server):

- ``chart.data`` — ``{type, request_id | null, subscription_id | null,
  symbol, timeframe, candles: [{time, open, high, low, close,
  tick_volume, spread?}], broker_time, received_at, complete, status}``.
  ``candles[]`` is the verbatim history array; for subscriptions only
  new/changed candles are sent (the last candle is re-sent while still
  forming). ``complete: false`` with ``status: "error"`` carries
  ``error_code`` + ``error_message`` instead of candles (§17.1.4).
- ``chart.tick`` — ``{type, subscription_id, request_id | null, symbol,
  ticks: [{time_ms, bid, ask}], broker_time, received_at}``. Batched:
  one frame per poll cycle, never one frame per tick.

Throttling / timeouts / idempotency (§17.1.3):

- Burst limit: 60 chart frames/min per device (chart frames have
  their own 60/min sub-budget inside the §2.5 WS 120/min budget).
  Exceeding it returns ``chart.error`` ``rate_limited``.
- Batching: one ``chart.data`` frame carries at most 1000 candles;
  one ``chart.tick`` frame at most 1000 ticks.
- Timeout: the server waits 30 s for a ``chart.data`` answer. On
  timeout it records ``chart_timeout`` and MAY retry once with a NEW
  ``request_id`` (a retry MUST NOT reuse the old ``request_id``).
- Idempotency: ``request_id`` is a uuidv7 and is the idempotency key
  for one-shot requests. A duplicate ``chart.request`` with the same
  ``request_id`` is answered from the in-memory cache of the previous
  answer — never executed as a second independent MT5 call.
  Subscriptions are NOT resumable across WS reconnects: the server
  re-subscribes, and a re-subscribe is a fresh subscription (new
  ``subscription_id``).

Rules:

- NO indicator computation in the Connector — raw candles/ticks/specs
  only. All indicator math, signal combination and decisions are
  server-side (§17.1.2).
- Symbol availability: any symbol the terminal has is requestable.
  An unknown symbol is an error (``symbol_not_found``), never an
  invented empty series.
- Market-Watch prerequisite chain (§17.1.2): before a chart/tick
  query for a symbol not in Market Watch, the Connector MUST run
  ``add_marketwatch_symbol`` first (local equivalent:
  ``mt5.symbol_select(symbol, True)`` — the existing local chart
  commands already do this; see :func:`bind_mt5_fetchers`) and MAY
  clean up with ``remove_marketwatch_symbol`` afterwards, skipping
  cleanup when it is blocked (a blocked cleanup is not an error).
  This module does NOT remove symbols after queries: removal is
  refused while the symbol is in use by a chart/position/order/EA
  (E199), and re-adding on every poll would churn Market Watch.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..errors import BridgeError

__all__ = [
    "CHART_FRAMES_PER_MINUTE",
    "CHART_MAX_COUNT",
    "CHART_MAX_SUBSCRIPTIONS",
    "CHART_POLL_DEFAULT_S",
    "CHART_POLL_MAX_S",
    "CHART_POLL_MIN_S",
    "CHART_REQUEST_TIMEOUT_S",
    "CHART_TIMEFRAMES",
    "ChartError",
    "ChartErrorCode",
    "ChartRateLimiter",
    "ChartService",
    "bind_mt5_fetchers",
    "broker_time_str",
    "build_chart_data_frame",
    "build_chart_error_frame",
    "build_chart_tick_frame",
    "validate_chart_request",
]

CHART_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")
CHART_MAX_COUNT = 1000
CHART_REQUEST_TIMEOUT_S = 30.0
CHART_MAX_SUBSCRIPTIONS = 20
CHART_POLL_MIN_S = 5
CHART_POLL_MAX_S = 3600
CHART_POLL_DEFAULT_S = 15
CHART_FRAMES_PER_MINUTE = 60
#: Candles fetched per subscription poll (latest N; deltas are derived
#: from these — enough to cover any poll interval up to 3600 s on M1).
SUBSCRIPTION_FETCH_COUNT = 100


class ChartErrorCode(str):
    """Error codes (§17.1.4). Values are plain strings by contract."""

    SYMBOL_NOT_FOUND = "symbol_not_found"
    SYMBOL_NOT_IN_MARKET_WATCH = "symbol_not_in_market_watch"
    UNSUPPORTED_TIMEFRAME = "unsupported_timeframe"
    MARKET_CLOSED = "market_closed"
    CHART_TIMEOUT = "chart_timeout"
    RATE_LIMITED = "rate_limited"
    SUBSCRIPTION_LIMIT = "subscription_limit"
    MANIFEST_MISSING_ACTION = "manifest_missing_action"


class ChartError(Exception):
    """A chart failure carrying a §17.1.4 error code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def broker_time_str(epoch_s: float, offset_minutes: int = 0, *, millis: bool = False) -> str:
    """Broker-local wall-time ISO string for a broker epoch.

    MT5 epochs are broker-server-local seconds; ``offset_minutes`` is
    the measured broker UTC offset. The result is the verbatim broker
    time the contract requires (e.g. ``"2026-11-16T00:00:00"`` for
    candles, ``"2026-11-16T19:53:00.034"`` for ticks) — never
    re-localized to UTC.
    """
    dt = datetime.fromtimestamp(epoch_s, tz=UTC) + timedelta(minutes=offset_minutes)
    return dt.isoformat(timespec="milliseconds" if millis else "seconds")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_chart_request(symbol: str, timeframe: str, count: int) -> tuple[str, str, int]:
    """Validate a one-shot/subscription chart request (§17.1.1).

    Returns the normalized ``(symbol, timeframe, count)``. Raises
    :class:`ChartError` with the §17.1.4 code on violation.

    NOTE on ``count``: §17.1.4 defines no error code for an
    out-of-range count (the server pages larger ranges itself), so
    out-of-range counts are CLAMPED to ``1..1000`` — the §17.1.3
    batching limit — with a warning, never answered with an invented
    code. A non-integer count is malformed and is answered with
    ``unsupported_timeframe`` (the §17.1.4 "client bug" code).
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ChartError(ChartErrorCode.SYMBOL_NOT_FOUND, "chart request has no symbol")
    timeframe = (timeframe or "").strip().upper()
    if timeframe not in CHART_TIMEFRAMES:
        raise ChartError(
            ChartErrorCode.UNSUPPORTED_TIMEFRAME,
            f"timeframe {timeframe!r} is outside M1..MN1",
        )
    if count is None:
        count = CHART_MAX_COUNT
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ChartError(ChartErrorCode.UNSUPPORTED_TIMEFRAME, "count is not an integer") from None
    if count < 1 or count > CHART_MAX_COUNT:
        logger.warning(
            "[CHART] count {} clamped to 1..{} (§17.1.3 batching limit)",
            count,
            CHART_MAX_COUNT,
        )
        count = max(1, min(CHART_MAX_COUNT, count))
    return symbol, timeframe, count


def build_chart_data_frame(
    *,
    request_id: str | None,
    subscription_id: str | None,
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    broker_time: str,
) -> dict[str, Any]:
    return {
        "type": "chart.data",
        "request_id": request_id,
        "subscription_id": subscription_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "broker_time": broker_time,
        "received_at": _utc_now_iso(),
        "complete": True,
        "status": "ok",
    }


def build_chart_error_frame(
    *,
    request_id: str | None,
    subscription_id: str | None,
    symbol: str,
    timeframe: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """``chart.data`` with ``status: "error"`` (§17.1.4).

    Error payloads MUST carry the ``request_id`` / ``subscription_id``
    so the server can correlate them, and MUST NOT echo raw MT5
    internals beyond the ``error_message`` string.
    """
    return {
        "type": "chart.data",
        "request_id": request_id,
        "subscription_id": subscription_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": [],
        "broker_time": _utc_now_iso(),
        "received_at": _utc_now_iso(),
        "complete": False,
        "status": "error",
        "error_code": code,
        "error_message": message,
    }


def build_chart_tick_frame(
    *,
    subscription_id: str | None,
    request_id: str | None,
    symbol: str,
    ticks: list[dict[str, Any]],
    broker_time: str,
) -> dict[str, Any]:
    return {
        "type": "chart.tick",
        "subscription_id": subscription_id,
        "request_id": request_id,
        "symbol": symbol,
        "ticks": ticks[:CHART_MAX_COUNT],
        "broker_time": broker_time,
        "received_at": _utc_now_iso(),
    }


class ChartRateLimiter:
    """60 chart frames/min sliding-window budget (§17.1.3)."""

    def __init__(self, limit_per_minute: int = CHART_FRAMES_PER_MINUTE) -> None:
        self.limit = limit_per_minute
        self._sent: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self._sent and self._sent[0] <= now - 60.0:
            self._sent.popleft()
        if len(self._sent) >= self.limit:
            return False
        self._sent.append(now)
        return True

    def would_allow(self, now: float | None = None) -> bool:
        """Budget check WITHOUT recording (Codex P2).

        ``allow()`` both checks and records — the subscription poll
        used it as a pre-check and ``_enqueue_chart`` charged the same
        frame again, so one poll consumed 2 budget entries per frame
        (4 for data+ticks) and the 60/min budget was really ~30/min.
        Pre-checks use this; only the single choke point
        (``_enqueue_chart`` -> ``allow()``) records a frame.
        """
        now = time.monotonic() if now is None else now
        while self._sent and self._sent[0] <= now - 60.0:
            self._sent.popleft()
        return len(self._sent) < self.limit

    @property
    def used(self) -> int:
        now = time.monotonic()
        while self._sent and self._sent[0] <= now - 60.0:
            self._sent.popleft()
        return len(self._sent)


@dataclass
class ChartSubscription:
    subscription_id: str
    symbol: str
    timeframe: str
    poll_interval_s: int
    include_ticks: bool
    next_poll_at: float = 0.0
    last_candle_time: str | None = None


@dataclass
class ChartService:
    """Handles ``chart.*`` frames with injectable MT5 fetchers.

    ``fetch_candles(symbol, timeframe, count, from_, to_)`` returns
    contract-shaped candles; ``fetch_ticks(symbol, count, from_, to_)``
    returns contract-shaped ticks; ``ensure_market_watch(symbol)``
    raises :class:`ChartError` (``symbol_not_found`` /
    ``symbol_not_in_market_watch``) when the prerequisite chain fails.
    The fetchers are synchronous (they run on the MT5 worker thread
    via ``run_in_threadpool`` at the call site).
    """

    fetch_candles: Callable[..., list[dict[str, Any]]]
    fetch_ticks: Callable[..., list[dict[str, Any]]] | None = None
    ensure_market_watch: Callable[[str], None] | None = None
    clock: Callable[[], float] = time.monotonic
    rate_limiter: ChartRateLimiter = field(default_factory=ChartRateLimiter)
    _subscriptions: dict[str, ChartSubscription] = field(default_factory=dict)
    _request_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    def handle_request(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        """Answer one ``chart.request`` (§17.1.1). Returns frames to send.

        A duplicate ``request_id`` is answered from the in-memory cache
        (§17.1.3) — never executed as a second independent MT5 call.
        """
        request_id = frame.get("request_id")
        symbol = str(frame.get("symbol") or "")
        timeframe = str(frame.get("timeframe") or "")
        if not isinstance(request_id, str) or not request_id:
            logger.warning("[CHART] chart.request without request_id — ignored (cannot correlate)")
            return []
        if request_id in self._request_cache:
            logger.debug("[CHART] duplicate chart.request {} — answering from cache", request_id)
            return [dict(self._request_cache[request_id])]
        try:
            symbol, timeframe, count = validate_chart_request(symbol, timeframe, frame.get("count", CHART_MAX_COUNT))
        except ChartError as exc:
            return [
                build_chart_error_frame(
                    request_id=request_id,
                    subscription_id=None,
                    symbol=symbol,
                    timeframe=timeframe,
                    code=exc.code,
                    message=exc.message,
                )
            ]
        try:
            candles = self._fetch_with_prerequisite(symbol, timeframe, count, frame.get("from"), frame.get("to"))
        except ChartError as exc:
            return [
                build_chart_error_frame(
                    request_id=request_id,
                    subscription_id=None,
                    symbol=symbol,
                    timeframe=timeframe,
                    code=exc.code,
                    message=exc.message,
                )
            ]
        data = build_chart_data_frame(
            request_id=request_id,
            subscription_id=None,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            broker_time=candles[-1]["time"] if candles else _utc_now_iso(),
        )
        if len(self._request_cache) >= 100:  # bounded idempotency cache
            self._request_cache.pop(next(iter(self._request_cache)))
        self._request_cache[request_id] = data
        frames: list[dict[str, Any]] = [data]
        if frame.get("include_ticks") and self.fetch_ticks is not None:
            try:
                ticks = self.fetch_ticks(symbol, count, frame.get("from"), frame.get("to"))
            except ChartError as exc:
                return [
                    build_chart_error_frame(
                        request_id=request_id,
                        subscription_id=None,
                        symbol=symbol,
                        timeframe=timeframe,
                        code=exc.code,
                        message=exc.message,
                    )
                ]
            frames.append(
                build_chart_tick_frame(
                    subscription_id=None,
                    request_id=request_id,
                    symbol=symbol,
                    ticks=ticks,
                    broker_time=ticks[-1]["time_ms"] if ticks else _utc_now_iso(),
                )
            )
        return frames

    def _fetch_with_prerequisite(
        self, symbol: str, timeframe: str, count: int, from_: Any, to: Any
    ) -> list[dict[str, Any]]:
        """Market-Watch prerequisite chain (§17.1.2) + one retry.

        The prerequisite failure is retried ONCE after the add
        (§17.1.4 ``symbol_not_in_market_watch`` — "Yes, once, after
        the prerequisite").
        """
        if self.ensure_market_watch is not None:
            try:
                self.ensure_market_watch(symbol)
            except ChartError as exc:
                if exc.code == ChartErrorCode.SYMBOL_NOT_IN_MARKET_WATCH:
                    logger.warning("[CHART] Market Watch add failed for {} — retrying once", symbol)
                    self.ensure_market_watch(symbol)  # raises again on failure
                else:
                    raise
        return self.fetch_candles(symbol, timeframe, count, from_, to)

    # ------------------------------------------------------------------
    def handle_subscribe(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Register a ``chart.subscribe``. Returns an error frame when
        refused (``subscription_limit``), else None (silent accept)."""
        subscription_id = frame.get("subscription_id")
        symbol = str(frame.get("symbol") or "")
        timeframe = str(frame.get("timeframe") or "")
        if not isinstance(subscription_id, str) or not subscription_id:
            logger.warning("[CHART] chart.subscribe without subscription_id — ignored")
            return None
        try:
            symbol, timeframe, _ = validate_chart_request(symbol, timeframe, 1)
        except ChartError as exc:
            return build_chart_error_frame(
                request_id=None,
                subscription_id=subscription_id,
                symbol=symbol,
                timeframe=timeframe,
                code=exc.code,
                message=exc.message,
            )
        if len(self._subscriptions) >= CHART_MAX_SUBSCRIPTIONS:
            return build_chart_error_frame(
                request_id=None,
                subscription_id=subscription_id,
                symbol=symbol,
                timeframe=timeframe,
                code=ChartErrorCode.SUBSCRIPTION_LIMIT,
                message=f"more than {CHART_MAX_SUBSCRIPTIONS} concurrent subscriptions on this device",
            )
        poll = frame.get("poll_interval_s", CHART_POLL_DEFAULT_S)
        try:
            poll = int(poll)
        except (TypeError, ValueError):
            poll = CHART_POLL_DEFAULT_S
        poll = max(CHART_POLL_MIN_S, min(CHART_POLL_MAX_S, poll))
        self._subscriptions[subscription_id] = ChartSubscription(
            subscription_id=subscription_id,
            symbol=symbol,
            timeframe=timeframe,
            poll_interval_s=poll,
            include_ticks=bool(frame.get("include_ticks", False)),
            next_poll_at=self.clock(),
        )
        logger.info("[CHART] subscribed {} {} every {}s (id {})", symbol, timeframe, poll, subscription_id[:8])
        return None

    def handle_unsubscribe(self, frame: dict[str, Any]) -> None:
        """End a subscription; unknown ids are a no-op (§17.1.1)."""
        subscription_id = frame.get("subscription_id")
        if isinstance(subscription_id, str) and self._subscriptions.pop(subscription_id, None) is not None:
            logger.info("[CHART] unsubscribed id {}", subscription_id[:8])

    def drop_all_subscriptions(self) -> None:
        """Subscriptions die with the WS session (§17.1.3); the server
        re-subscribes on reconnect with fresh ids."""
        if self._subscriptions:
            logger.info("[CHART] dropping {} subscriptions (WS session ended)", len(self._subscriptions))
            self._subscriptions.clear()

    # ------------------------------------------------------------------
    def poll_due(self, now: float | None = None) -> list[ChartSubscription]:
        """Subscriptions whose poll interval has elapsed."""
        now = self.clock() if now is None else now
        due = [s for s in self._subscriptions.values() if now >= s.next_poll_at]
        for sub in due:
            sub.next_poll_at = now + sub.poll_interval_s
        return due

    def build_delta(self, sub: ChartSubscription, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Delta-only candles for a subscription poll (§17.1.1).

        Only new/changed candles are sent; the last candle is re-sent
        while still forming (its ``time`` equals the last sent time).
        """
        if sub.last_candle_time is None:
            delta = list(candles)
        else:
            delta = [c for c in candles if str(c.get("time", "")) >= sub.last_candle_time]
        if candles:
            sub.last_candle_time = max(str(c.get("time", "")) for c in candles)
        return delta


def bind_mt5_fetchers(ctx: Any) -> tuple[Callable, Callable, Callable]:
    """Build (fetch_candles, fetch_ticks, ensure_market_watch) over the
    existing local MT5 driver (no driver rewrite, no MCP transport).

    Candle/tick mapping (contract §17.1.1 shapes, broker time verbatim):

    - Local ``get_chart_history`` bars carry ``time`` (broker epoch),
      ``open/high/low/close``, ``tick_volume`` and optional ``spread``.
      The contract candle ``time`` is the broker-local ISO string
      reconstructed from the epoch + broker offset; ``spread`` is
      included only when the terminal reports it.
    - Local tick history carries ``time_msc``/``bid``/``ask``; the
      contract tick ``time_ms`` is the broker-local ISO string with
      milliseconds.
    - ``ensure_market_watch`` checks ``symbol_info`` first (unknown
      symbol → ``symbol_not_found``, never an invented empty series)
      and then ``symbol_select(symbol, True)`` (the §17.1.2
      prerequisite chain; a failed add → ``symbol_not_in_market_watch``).
    - A fetch that fails after the prerequisite passed means the
      terminal has no fresh data for a known symbol → ``market_closed``
      (distinct from a data-path error, §17.1.4).
    """
    from ..engine.charts import ChartHistoryRequest, TicksHistoryRequest, get_chart_history, get_chart_ticks_history

    offset = ctx.settings.broker_utc_offset_minutes

    def ensure_market_watch(symbol: str) -> None:
        ctx.driver.ensure_connected()

        def job(mt5: Any):
            info = mt5.symbol_info(symbol)
            if info is None:
                return "not_found"
            if getattr(info, "select", False):
                return "ok"
            return "selected" if mt5.symbol_select(symbol, True) else "add_failed"

        outcome = ctx.driver.run(job, op="symbol_select", context={"symbol": symbol})
        if outcome == "not_found":
            raise ChartError(
                ChartErrorCode.SYMBOL_NOT_FOUND,
                f"symbol {symbol!r} does not exist on this broker/terminal",
            )
        if outcome == "add_failed":
            raise ChartError(
                ChartErrorCode.SYMBOL_NOT_IN_MARKET_WATCH,
                f"symbol {symbol!r} could not be added to Market Watch",
                retryable=True,
            )

    def fetch_candles(symbol: str, timeframe: str, count: int, from_: Any, to: Any) -> list[dict[str, Any]]:
        try:
            result = get_chart_history(
                ctx,
                ChartHistoryRequest(
                    symbol=symbol,
                    timeframe=timeframe,
                    count=count,
                    from_utc=str(from_) if from_ is not None else None,
                ),
            )
        except BridgeError as exc:
            raise ChartError(
                ChartErrorCode.MARKET_CLOSED,
                f"no fresh candle data for {symbol!r} ({timeframe}): {exc.message}",
                retryable=True,
            ) from exc
        candles: list[dict[str, Any]] = []
        for bar in result.get("bars", []):
            candle = {
                "time": broker_time_str(float(bar["time"]), offset),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "tick_volume": bar.get("tick_volume", 0),
            }
            if bar.get("spread") is not None:
                candle["spread"] = bar["spread"]
            candles.append(candle)
        return candles

    def fetch_ticks(symbol: str, count: int, from_: Any, to: Any) -> list[dict[str, Any]]:
        try:
            result = get_chart_ticks_history(
                ctx,
                TicksHistoryRequest(
                    symbol=symbol,
                    count=count,
                    from_utc=str(from_) if from_ is not None else None,
                    to_utc=str(to) if to is not None else None,
                ),
            )
        except BridgeError as exc:
            raise ChartError(
                ChartErrorCode.MARKET_CLOSED,
                f"no fresh tick data for {symbol!r}: {exc.message}",
                retryable=True,
            ) from exc
        ticks: list[dict[str, Any]] = []
        for tick in result.get("ticks", []):
            msc = tick.get("time_msc") or 0
            ticks.append(
                {
                    "time_ms": broker_time_str(float(msc) / 1000.0, offset, millis=True),
                    "bid": tick.get("bid"),
                    "ask": tick.get("ask"),
                }
            )
        return ticks

    return fetch_candles, fetch_ticks, ensure_market_watch

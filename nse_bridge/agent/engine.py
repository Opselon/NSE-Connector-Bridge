"""AgentService — the AI Agent orchestrator.

Pipeline: gather bridge data → engines (technical/news/math) → COACH →
9-router → optional LLM review → decision. Execution only happens when the
user's mode is `auto` AND the router passes AND the math engine sizes the
trade; every decision is recorded (and mirrored to the platform's
decision-audit endpoint when the server channel is configured).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..engine.common import BridgeContext
from ..errors import BridgeError, ErrorCode
from .coach import Coach
from .engines import MathEngine, NewsEngine, TechnicalEngine
from .llm import LlmClient
from .router9 import Router9

MODES = ("observe", "advisory", "auto")


@dataclass
class AgentConfig:
    mode: str = "observe"
    aggression: int = 45  # 0-100
    symbol: str = "XAUUSD"
    auto_execute: bool = False
    llm: LlmClient | None = None
    news_items: list[dict[str, Any]] = field(default_factory=list)


class AgentService:
    """Singleton per bridge process; thread-safe via a lock (MT5 worker)."""

    def __init__(self, ctx: BridgeContext) -> None:
        self.ctx = ctx
        self.config = AgentConfig()
        self.coach = Coach()
        self._lock = threading.Lock()
        self._last_analysis: dict[str, Any] | None = None
        self._executed_today = 0
        self._last_executed_pnl: float | None = None

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def configure(
        self,
        *,
        mode: str | None = None,
        aggression: int | None = None,
        symbol: str | None = None,
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
        llm_model: str | None = None,
        news_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if mode is not None:
                if mode not in MODES:
                    raise BridgeError(
                        ErrorCode.VALIDATION, f"mode must be one of {MODES}", http_status=422
                    )
                self.config.mode = mode
            if aggression is not None:
                self.config.aggression = max(0, min(100, int(aggression)))
            if symbol:
                self.config.symbol = symbol.strip().upper()
            if llm_base_url is not None or llm_api_key is not None or llm_model is not None:
                base = llm_base_url or (
                    self.config.llm.base_url if self.config.llm else ""
                )
                key = llm_api_key or (self.config.llm.api_key if self.config.llm else "")
                model = llm_model or (
                    self.config.llm.model if self.config.llm else "gpt-4o-mini"
                )
                self.config.llm = LlmClient.from_config(
                    type(
                        "Cfg",
                        (),
                        {
                            "agent_llm_base_url": base,
                            "agent_llm_api_key": key,
                            "agent_llm_model": model,
                        },
                    )()
                )
            if news_items is not None:
                self.config.news_items = news_items
            return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "aggression": self.config.aggression,
            "symbol": self.config.symbol,
            "risk_pct_per_trade": MathEngine.risk_fraction(self.config.aggression),
            "min_confidence": MathEngine.min_confidence(self.config.aggression),
            "sl_atr_mult": MathEngine.stop_atr_multiplier(self.config.aggression),
            "tp_atr_mult": MathEngine.reward_atr_multiplier(self.config.aggression),
            "llm_review": self.config.llm is not None,
            "auto_execute": self.config.auto_execute and self.config.mode == "auto",
            "executed_today": self._executed_today,
            "coach_consecutive_losses": self.coach._consecutive_losses,
        }

    def record_closed_trade(self, pnl: float) -> None:
        self.coach.record_trade(pnl)

    # ------------------------------------------------------------------
    # data gathering
    # ------------------------------------------------------------------
    def _gather(self, symbol: str) -> Any:
        from ..engine.common import account_info_model
        from ..engine.charts import ChartHistoryRequest, get_chart_history
        from ..engine.market import get_market_spread_history, get_market_tick
        from ..engine.positions import PositionsRequest, get_trading_open_positions
        from ..engine.symbols import _fresh_tick

        tick = _fresh_tick(self.ctx, symbol)
        bars15 = get_chart_history(
            self.ctx, ChartHistoryRequest(symbol=symbol, timeframe="M15", count=150)
        )["bars"]
        bars1h = get_chart_history(
            self.ctx, ChartHistoryRequest(symbol=symbol, timeframe="H1", count=120)
        )["bars"]
        acc = account_info_model(self.ctx)
        positions = get_trading_open_positions(self.ctx, PositionsRequest())["positions"]
        spread_row = get_market_spread_history(
            self.ctx, {"symbol": symbol, "minutes": 30, "limit": 10}
        )
        baseline = spread_row.get("avg_spread")

        daily_pnl = sum(float(p.get("profit") or 0.0) for p in positions)
        margin_level = acc.margin_level if acc.margin_level else None
        return (
            tick,
            bars15,
            bars1h,
            acc,
            positions,
            float(baseline) if baseline else None,
            daily_pnl,
            margin_level,
        )

    def _build_snapshot(self, symbol: str) -> tuple[Any, Any]:
        (
            tick,
            bars15,
            bars1h,
            acc,
            positions,
            baseline,
            daily_pnl,
            margin_level,
        ) = self._gather(symbol)
        snap = NewsEngine  # noqa: F841 — type reference only
        from .engines import MarketSnapshot

        snapshot = MarketSnapshot(
            symbol=symbol,
            price=float(tick.bid or 0.0),
            spread=float(tick.spread_points or 0.0),
            bars_m15=bars15,
            bars_h1=bars1h,
            balance=float(acc.balance or 0.0),
            equity=float(acc.equity or 0.0),
            margin_free=float(acc.margin_free or 0.0),
            margin_level=margin_level,
            open_positions=len(positions),
            daily_pnl=daily_pnl,
            news_items=self.config.news_items,
        )
        return snapshot, baseline

    # ------------------------------------------------------------------
    # the analyze pipeline
    # ------------------------------------------------------------------
    def analyze(self, symbol: str | None = None) -> dict[str, Any]:
        with self._lock:
            return self._analyze_locked(symbol)

    def _analyze_locked(self, symbol: str | None) -> dict[str, Any]:
        symbol = (symbol or self.config.symbol).strip().upper()
        aggression = self.config.aggression
        snapshot, spread_baseline = self._build_snapshot(symbol)

        # engines
        technical = TechnicalEngine().evaluate(snapshot)
        news = NewsEngine().evaluate(snapshot)
        math_verdict = MathEngine().evaluate(snapshot, aggression)
        verdicts = [*technical, *news, math_verdict]

        # coach — has the proposal a stop? yes: the math engine always ships one.
        coach_verdict = self.coach.coach(snapshot, has_stop_loss=True, proposals_today=self._executed_today)

        # router
        route = Router9().route(snapshot, verdicts, coach_verdict.score, spread_baseline)

        # math sizing for the proposed trade
        atr_hint = next(
            (v.detail.get("atr") for v in verdicts if v.engine == "volatility" and v.detail.get("atr")),
            None,
        )
        proposal = self._build_proposal(symbol, snapshot, route.direction_hint, atr_hint, aggression)

        # optional LLM review
        llm_verdict = None
        if self.config.llm is not None:
            llm_verdict = self.config.llm.review(
                symbol,
                route.direction_hint,
                route.confidence,
                [g.__dict__ for g in route.gates],
                proposal["math"],
                "; ".join(coach_verdict.messages) or coach_verdict.verdict,
                _news_summary(snapshot.news_items),
                route.vetoed_by,
            )

        # decision
        min_conf = MathEngine.min_confidence(aggression)
        llm_blocks = (
            llm_verdict is not None
            and llm_verdict["verdict"] == "NO_TRADE"
        )
        trade_approved = (
            not route.vetoed_by
            and route.confidence >= min_conf
            and route.direction_hint != "none"
            and coach_verdict.verdict != "veto"
            and not llm_blocks
        )
        decision = {
            "decision": "TRADE" if trade_approved else "NO_TRADE",
            "reason": self._reason(route, coach_verdict.verdict, min_conf, llm_verdict),
            "symbol": symbol,
            "direction": route.direction_hint,
            "aggression": aggression,
            "mode": self.config.mode,
            "confidence": round(route.confidence, 1),
            "min_confidence": min_conf,
            "gates": [g.__dict__ for g in route.gates],
            "vetoed_by": route.vetoed_by,
            "engines": [
                {"engine": v.engine, "score": v.score, "summary": v.summary, "detail": v.detail}
                for v in verdicts
            ],
            "proposal": proposal if trade_approved else None,
            "llm": llm_verdict,
            "coach": {
                "verdict": coach_verdict.verdict,
                "score": round(coach_verdict.score, 1),
                "messages": coach_verdict.messages,
                "stats": coach_verdict.stats,
            },
            "generated_at": time.time(),
        }
        self._last_analysis = decision
        self._mirror_to_platform(decision)
        return decision

    def _build_proposal(
        self,
        symbol: str,
        snapshot: Any,
        direction: str,
        atr_hint: float | None,
        aggression: int,
    ) -> dict[str, Any]:
        """Math-engine sizing: risk % → lots, ATR-based SL/TP (broker-truth
        sizing verified via calculate_position_size at execution)."""
        risk_pct = MathEngine.risk_fraction(aggression)
        sl_mult = MathEngine.stop_atr_multiplier(aggression)
        tp_mult = MathEngine.reward_atr_multiplier(aggression)
        atr_val = atr_hint or abs(snapshot.price) * 0.0015
        entry = snapshot.price
        side = "buy" if direction == "buy" else "sell"
        sl = entry - sl_mult * atr_val if side == "buy" else entry + sl_mult * atr_val
        tp = entry + tp_mult * atr_val if side == "buy" else entry - tp_mult * atr_val
        return {
            "side": side,
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "risk_pct": risk_pct,
            "math": {
                "risk_pct": risk_pct,
                "entry": round(entry, 5),
                "sl": round(sl, 5),
                "tp": round(tp, 5),
                "sl_atr_mult": sl_mult,
                "tp_atr_mult": tp_mult,
                "atr": round(atr_val, 6),
                "session": _session_name(),
            },
            "note": "Volume is computed via calculate_position_size at execution (broker-truth tick value).",
        }

    def _reason(self, route, coach_verdict_str: str, min_conf: float, llm_verdict) -> str:
        if route.vetoed_by:
            return f"vetoed by gates: {', '.join(route.vetoed_by)}"
        if route.confidence < min_conf:
            return f"confidence {route.confidence:.1f} below required {min_conf} at aggression {self.config.aggression}"
        if route.direction_hint == "none":
            return "no directional agreement between trend and momentum"
        if coach_verdict_str == "veto":
            return "COACH veto — discipline rules breached"
        if llm_verdict is not None and llm_verdict["verdict"] == "NO_TRADE":
            return f"LLM review: {llm_verdict.get('critique', 'no trade')}"
        return "router confidence above threshold, coach pass, no vetoes"

    # ------------------------------------------------------------------
    # execution (auto mode only, gated)
    # ------------------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        """Execute the last approved proposal (auto mode only)."""
        if self.config.mode != "auto":
            raise BridgeError(
                ErrorCode.VALIDATION,
                f"execution requires mode 'auto' (current: {self.config.mode})",
                http_status=422,
            )
        if not self.config.auto_execute:
            raise BridgeError(
                ErrorCode.VALIDATION,
                "auto_execute consent disabled — enable it in the AI Agent settings",
                http_status=422,
            )
        with self._lock:
            if not self._last_analysis or self._last_analysis["decision"] != "TRADE":
                raise BridgeError(
                    ErrorCode.VALIDATION, "run analyze first — last decision was not TRADE",
                    http_status=422,
                )
            proposal = self._last_analysis["proposal"]
            symbol = self._last_analysis["symbol"]
            side = proposal["side"]

            # broker-truth sizing from the risk commands
            risk_pct = proposal["risk_pct"]
            stop_distance = abs(proposal["entry"] - proposal["sl"])
            sizing = self._call(
                "calculate_position_size",
                {
                    "symbol": symbol,
                    "side": side,
                    "risk_percent": risk_pct,
                    "stop_price": proposal["sl"],
                },
            )
            lots = float(sizing["lots"])

            result = self._call(
                "trade_send_market_order",
                {
                    "symbol": symbol,
                    "side": side,
                    "volume": lots,
                    "sl": proposal["sl"],
                    "tp": proposal["tp"],
                    "comment": "NSE_AGENT",
                },
            )
            self._executed_today += 1
            logger.info(
                "[AGENT] executed {} {} {} lots @ {} (SL {} TP {}) — retcode {}",
                side, symbol, lots, result.get("price"), proposal["sl"], proposal["tp"],
                result.get("retcode"),
            )
            return {
                "executed": True,
                "symbol": symbol,
                "side": side,
                "volume": lots,
                "sl": proposal["sl"],
                "tp": proposal["tp"],
                "retcode": result.get("retcode"),
                "price": result.get("price"),
                "order_ticket": result.get("order_ticket"),
                "sizing": sizing,
            }

    def _call(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Invoke a bridge command through the registry (agent is a local
        client of the same command surface the API and platform use)."""
        from ..engine.registry import REGISTRY

        spec = next((c for c in REGISTRY if c.name == name), None)
        if spec is None:
            raise BridgeError(ErrorCode.NOT_FOUND, f"command '{name}' not registered")
        return spec.handler(self.ctx, params)

    def _mirror_to_platform(self, decision: dict[str, Any]) -> None:
        """Best-effort mirror of the decision to the platform audit endpoint."""
        channel = getattr(self.ctx, "server_channel", None) or getattr(
            getattr(self.ctx, "app_channel", None), "_self", None
        )
        # The platform mirror rides the server channel when configured; the
        # channel's own event stream already carries agent decisions as
        # trade_event/status frames — this hook posts the full decision via
        # the REST audit endpoint when a REST base is configured.
        base = getattr(self.ctx.settings, "server_rest_base", "")
        token = getattr(self.ctx.settings, "server_token", "")
        account_id = None
        if not base or not token:
            return
        try:
            import httpx

            acc = self._call("get_trading_account_info", {})
            account_login = acc["account"]["login"]
            account_id = f"mt5-{account_login}"
            payload = {
                "trading_account_id": account_id,
                "symbol": decision["symbol"],
                "timeframe": "M15",
                "indicator_inputs": {
                    "gates": decision["gates"],
                    "engines": decision["engines"],
                },
                "proposed_action": (
                    "open_order" if decision["decision"] == "TRADE" else "no_trade"
                ),
                "proposed_params": decision["proposal"],
                "risk_validation": {
                    "passed": decision["decision"] == "TRADE",
                    "checks": decision["gates"],
                    "reason": decision["reason"],
                },
            }
            httpx.post(
                f"{base}/api/v1/connector/ai-agent/decisions",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 — mirroring is best-effort
            logger.debug("[AGENT] platform decision mirror skipped: {}", exc)


def _news_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "none"
    return "; ".join(str(i.get("title", ""))[:60] for i in items[:5])


def _session_name() -> str:
    hour = time.gmtime().tm_hour
    if 12 <= hour < 17:
        return "London/NY overlap"
    if 7 <= hour < 21:
        return "London or New York"
    if 0 <= hour < 7:
        return "Asian session"
    return "rollover"

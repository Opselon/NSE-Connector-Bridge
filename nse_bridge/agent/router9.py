"""The 9-Router — nine deterministic gates over the engine verdicts.

Each gate converts engine evidence into a 0-100 score with a verdict:
    pass  (>= 65) — contributes positively
    warn  (45..65) — contributes with hesitation
    veto  (< 45 or an explicit block) — the trade dies here

Final confidence = weighted mean of gate scores. A proposal survives only
when no gate vetoes AND confidence >= min_confidence(aggression).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .engines import MarketSnapshot, EngineVerdict

# Gate weights sum to 100 — the confidence is a weighted mean of scores.
GATE_WEIGHTS: dict[str, float] = {
    "trend": 18.0,
    "momentum": 14.0,
    "volatility": 10.0,
    "session": 8.0,
    "spread": 8.0,
    "exposure": 12.0,
    "news": 10.0,
    "drawdown": 10.0,
    "coach": 10.0,
}


@dataclass
class Gate:
    name: str
    score: float
    verdict: str  # pass | warn | veto
    detail: str


@dataclass
class RouteResult:
    confidence: float
    gates: list[Gate]
    vetoed_by: list[str]
    direction_hint: str  # "buy" | "sell" | "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 1),
            "gates": [asdict(g) for g in self.gates],
            "vetoed_by": self.vetoed_by,
            "direction_hint": self.direction_hint,
        }


def _engine_score(verdicts: list[EngineVerdict], name: str, default: float = 50.0) -> tuple[float, str, dict[str, Any]]:
    for v in verdicts:
        if v.engine == name:
            return v.score, v.summary, v.detail
    return default, "no data", {}


def forex_session_score() -> tuple[float, str]:
    """London/NY overlap (12-16 UTC) is the prime session; Asia is thin."""
    hour = time.gmtime().tm_hour
    if 12 <= hour < 17:
        return 90.0, "London/NY overlap"
    if 7 <= hour < 21:
        return 75.0, "London or New York session"
    if 0 <= hour < 7:
        return 55.0, "Asian session — thinner moves"
    return 40.0, "post-NY close — rollover hour"


class Router9:
    """The nine gates. Pure function of (snapshot, engine verdicts, coach)."""

    def route(
        self,
        snap: MarketSnapshot,
        verdicts: list[EngineVerdict],
        coach_score: float,
        spread_baseline: float | None = None,
    ) -> RouteResult:
        gates: list[Gate] = []
        vetoed: list[str] = []

        def add(name: str, score: float, detail: str) -> None:
            verdict = "pass" if score >= 65 else ("warn" if score >= 45 else "veto")
            if verdict == "veto":
                vetoed.append(name)
            gates.append(Gate(name, round(score, 1), verdict, detail))

        # 1. trend ---------------------------------------------------------
        s, d, _ = _engine_score(verdicts, "trend")
        add("trend", s, d)

        # 2. momentum ------------------------------------------------------
        s, d, _ = _engine_score(verdicts, "momentum")
        add("momentum", s, d)

        # 3. volatility ----------------------------------------------------
        s, d, _ = _engine_score(verdicts, "volatility")
        add("volatility", s, d)

        # 4. session -------------------------------------------------------
        s, d = forex_session_score()
        add("session", s, d)

        # 5. spread --------------------------------------------------------
        if snap.spread > 0 and snap.price > 0:
            rel = snap.spread / snap.price * 10000  # spread in bps-ish
            if spread_baseline:
                ratio = snap.spread / max(spread_baseline, 1e-9)
                spread_score = 80.0 if ratio <= 1.5 else (50.0 if ratio <= 2.5 else 25.0)
                detail = f"spread {snap.spread:.5f} (x{ratio:.2f} of baseline)"
            else:
                spread_score = 80.0 if rel < 5 else (50.0 if rel < 12 else 25.0)
                detail = f"spread {snap.spread:.5f} ({rel:.1f} bps)"
            add("spread", spread_score, detail)
        else:
            add("spread", 50.0, "no spread data")

        # 6. exposure ------------------------------------------------------
        s, d, _ = _engine_score(verdicts, "math")
        add("exposure", s, d)

        # 7. news ----------------------------------------------------------
        s, d, _ = _engine_score(verdicts, "news")
        add("news", s, d)

        # 8. drawdown ------------------------------------------------------
        daily_dd = abs(min(0.0, snap.daily_pnl)) / max(1.0, snap.balance) * 100.0
        dd_score = 90.0 if daily_dd < 1.0 else (70.0 if daily_dd < 2.5 else (45.0 if daily_dd < 4.0 else 15.0))
        add("drawdown", dd_score, f"daily drawdown {daily_dd:.2f}% (PnL {snap.daily_pnl:+.2f})")

        # 9. coach ---------------------------------------------------------
        add("coach", coach_score, "COACH discipline verdict")

        # confidence: weighted mean of scores
        total = sum(GATE_WEIGHTS.values())
        confidence = sum(
            g.score * GATE_WEIGHTS[g.name] for g in gates
        ) / total

        # direction hint from trend + momentum (routing, not execution)
        trend = next((g for g in gates if g.name == "trend"), None)
        momentum = next((g for g in gates if g.name == "momentum"), None)
        direction = "none"
        if trend and momentum:
            t_bull = trend.score > 55
            m_bull = momentum.score > 55
            if t_bull and m_bull:
                direction = "buy"
            elif (not t_bull) and (not m_bull):
                direction = "sell"

        return RouteResult(confidence, gates, vetoed, direction)

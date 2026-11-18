"""COACH system — discipline enforcement and lessons.

The coach is deliberately rule-based: it enforces the boring, life-saving
rules (stop-loss presence, overtrading, revenge trading, drawdown breaks)
and returns a 0-100 discipline score that the 9-router treats as a gate.
LLM narration (when configured) only *phrases* the verdict — it can never
flip a veto.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .engines import MarketSnapshot


@dataclass
class CoachVerdict:
    score: float  # 0-100 discipline health
    verdict: str  # pass | warn | veto
    messages: list[str]
    stats: dict[str, Any]


class Coach:
    """Rule-based trading coach over the account snapshot + recent history."""

    def __init__(self) -> None:
        # trade timestamps of THIS session (in-memory by design; the journal
        # of record lives on the platform)
        self._trade_times: list[float] = []
        self._consecutive_losses = 0
        self._session_start = time.time()

    def record_trade(self, closed_pnl: float) -> None:
        self._trade_times.append(time.time())
        self._consecutive_losses = (
            self._consecutive_losses + 1 if closed_pnl < 0 else 0
        )

    def coach(
        self,
        snap: MarketSnapshot,
        has_stop_loss: bool,
        proposals_today: int = 0,
    ) -> CoachVerdict:
        messages: list[str] = []
        score = 100.0

        # 1. no stop-loss — the cardinal sin
        if not has_stop_loss:
            score -= 60
            messages.append(
                "No stop-loss on the proposal — every trade ships with an SL. "
                "Set it or walk away."
            )

        # 2. overtrading: > 10 bridge trades in the last hour
        hour_ago = time.time() - 3600
        recent = [t for t in self._trade_times if t >= hour_ago]
        if len(recent) >= 10:
            score -= 25
            messages.append(
                f"Overtrading: {len(recent)} trades in the last hour. "
                "Reduce frequency; quality over quantity."
            )

        # 3. revenge trading: 3+ consecutive losses
        if self._consecutive_losses >= 3:
            score -= 30
            messages.append(
                f"{self._consecutive_losses} consecutive losses — size down 50% "
                "or take a 30-minute break. The market will still be here."
            )

        # 4. daily drawdown break — 4%+ is a hard stop for the day
        daily_dd = abs(min(0.0, snap.daily_pnl)) / max(1.0, snap.balance) * 100.0
        if daily_dd >= 4.0:
            score -= 60
            messages.append(
                f"Daily drawdown {daily_dd:.1f}% — stop for today. "
                "Protect the account; the edge is gone for now."
            )
        elif daily_dd >= 2.5:
            score -= 15
            messages.append(
                f"Daily drawdown {daily_dd:.1f}% — cut size in half until it recovers."
            )

        # 5. margin safety
        if snap.margin_level is not None and snap.margin_level < 200:
            score -= 20
            messages.append(
                f"Margin level {snap.margin_level:.0f}% — free up margin before adding risk."
            )

        # 6. session hour lesson
        hour = time.gmtime().tm_hour
        if hour in (21, 22, 23) and hour >= 21:
            messages.append("Rollover hour — spreads widen; wait for liquidity if unsure.")

        verdict = "pass" if score >= 65 else ("warn" if score >= 45 else "veto")
        return CoachVerdict(
            max(0.0, score),
            verdict,
            messages,
            {
                "trades_last_hour": len(recent),
                "consecutive_losses": self._consecutive_losses,
                "daily_drawdown_pct": round(daily_dd, 2),
                "session_minutes": round((time.time() - self._session_start) / 60, 1),
                "proposals_today": proposals_today,
            },
        )

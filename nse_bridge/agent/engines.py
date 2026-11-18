"""Multi-engine analysis: technical, news, and risk math.

Every engine is pure math over inputs the bridge already serves (OHLCV via
get_chart_history, ticks via get_market_tick, account via the account
commands, sizing via the risk commands). No engine dispatches trades —
the router + agent decide, the bridge executes.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# indicator math (pure functions — unit-testable)
# ---------------------------------------------------------------------------


def ema(values: list[float], period: int) -> list[float]:
    """Standard EMA seeded with the SMA of the first `period` values."""
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI; None when the series is too short."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    """Wilder's ATR over bars with high/low/close."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line_last, signal_last, histogram_last) or None."""
    if len(values) < slow + signal:
        return None
    fast_e, slow_e = ema(values, fast), ema(values, slow)
    line = [f - s for f, s in zip(fast_e[-(len(values) - slow + 1):], slow_e)]
    signal_e = ema(line, signal)
    if not line or not signal_e:
        return None
    hist = line[-1] - signal_e[-1]
    return line[-1], signal_e[-1], hist


def bollinger(values: list[float], period: int = 20, mult: float = 2.0):
    """Returns (mid, upper, lower, %b) or None."""
    if len(values) < period:
        return None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    sd = math.sqrt(variance)
    upper, lower = mid + mult * sd, mid - mult * sd
    price = values[-1]
    band = upper - lower
    pct_b = (price - lower) / band if band else 0.5
    return mid, upper, lower, max(0.0, min(1.0, pct_b))


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


@dataclass
class EngineVerdict:
    """One engine's output: a score 0-100 plus narrative detail."""

    engine: str
    score: float
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketSnapshot:
    """Everything the engines need, gathered from bridge commands."""

    symbol: str
    price: float
    spread: float
    bars_m15: list[dict[str, Any]]
    bars_h1: list[dict[str, Any]]
    balance: float
    equity: float
    margin_free: float
    margin_level: float | None
    open_positions: int
    daily_pnl: float
    news_items: list[dict[str, Any]] = field(default_factory=list)


class TechnicalEngine:
    """Trend / momentum / volatility over M15 + H1 candles."""

    name = "technical"

    def evaluate(self, snap: MarketSnapshot) -> list[EngineVerdict]:
        closes15 = [float(b["close"]) for b in snap.bars_m15]
        closes1h = [float(b["close"]) for b in snap.bars_h1]
        verdicts: list[EngineVerdict] = []

        # -- trend: EMA stack on H1 --------------------------------------
        e9, e21, e50 = ema(closes1h, 9), ema(closes1h, 21), ema(closes1h, 50)
        if e9 and e21 and e50:
            bull = e9[-1] > e21[-1] > e50[-1]
            bear = e9[-1] < e21[-1] < e50[-1]
            slope = (e9[-1] - e9[-3]) / e9[-3] if len(e9) >= 3 and e9[-3] else 0.0
            score = 70.0 if bull else (30.0 if bear else 50.0)
            score += max(-15.0, min(15.0, slope * 1000.0))
            verdicts.append(
                EngineVerdict(
                    "trend",
                    max(0.0, min(100.0, score)),
                    "bullish stack" if bull else ("bearish stack" if bear else "mixed EMAs"),
                    {"ema9": round(e9[-1], 5), "ema21": round(e21[-1], 5), "ema50": round(e50[-1], 5)},
                )
            )

        # -- momentum: RSI + MACD on M15 ----------------------------------
        r = rsi(closes15)
        m = macd(closes15)
        if r is not None and m is not None:
            # MACD side sets the base; RSI extremes are danger only on
            # DIVERGENCE (RSI exhausted against the MACD direction) — a
            # trending runner with RSI pinned high and MACD positive is
            # strong momentum, not exhaustion.
            base = 55.0 + (15.0 if m[2] > 0 else -15.0)
            if 40.0 <= r <= 75.0:
                base += 10.0
            elif (r > 85.0 and m[2] < 0) or (r < 15.0 and m[2] > 0):
                base -= 20.0  # true divergence — the market disagrees
            verdicts.append(
                EngineVerdict(
                    "momentum",
                    max(0.0, min(100.0, base)),
                    f"RSI {r:.1f}, MACD hist {m[2]:+.6f}",
                    {"rsi": round(r, 2), "macd_hist": round(m[2], 6)},
                )
            )

        # -- volatility: ATR sanity — dead or exploding markets are bad ----
        a = atr(snap.bars_m15)
        avg_range = (
            sum(abs(float(b["high"]) - float(b["low"])) for b in snap.bars_m15[-50:])
            / max(1, len(snap.bars_m15[-50:]))
            if snap.bars_m15
            else 0.0
        )
        if a and avg_range > 0:
            ratio = a / avg_range
            score = 70.0 if 0.5 <= ratio <= 2.0 else 35.0
            verdicts.append(
                EngineVerdict(
                    "volatility",
                    score,
                    f"ATR {a:.5f} vs avg range {avg_range:.5f} (x{ratio:.2f})",
                    {"atr": round(a, 6), "ratio": round(ratio, 2)},
                )
            )
        return verdicts


class NewsEngine:
    """Keyword-sentiment over platform-provided news items.

    Items shape: {"title": str, "impact": "high|medium|low",
                  "currency": str, "time_utc": str} — what the platform's
    news feed serves; manual items are accepted for what-if analysis.
    """

    name = "news"

    HIGH_IMPACT_BLOCK_MINUTES = 30

    def evaluate(self, snap: MarketSnapshot) -> list[EngineVerdict]:
        items = snap.news_items
        if not items:
            return [
                EngineVerdict(
                    "news", 70.0, "no news items provided — sentiment neutral",
                    {"items": 0},
                )
            ]
        now = time.time()
        blockers = []
        sentiment = 0.0
        for item in items:
            impact = str(item.get("impact", "low")).lower()
            title = str(item.get("title", "")).lower()
            for kw, weight in (
                ("rate cut", -1.0), ("rate hike", 1.0), ("hike", 0.6), ("cut", -0.6),
                ("inflation", 0.3), ("recession", -0.8), ("war", -0.9),
                ("nfp", 0.5), ("cpi", 0.5), ("fomc", 0.7), (" hawkish", 0.8),
                (" dovish", -0.8), ("crash", -0.9), ("record high", 0.4),
            ):
                if kw in title:
                    sentiment += weight * (1.0 if impact == "high" else 0.5 if impact == "medium" else 0.2)
            if impact == "high" and item.get("time_utc"):
                try:
                    from datetime import datetime, UTC

                    t = datetime.fromisoformat(str(item["time_utc"]).replace("Z", "+00:00"))
                    age_min = (now - t.timestamp()) / 60.0
                    if abs(age_min) <= self.HIGH_IMPACT_BLOCK_MINUTES:
                        blockers.append({"title": str(item.get("title"))[:80], "age_min": round(age_min, 1)})
                except ValueError:
                    pass
        if blockers:
            return [
                EngineVerdict(
                    "news", 5.0,
                    "high-impact news within ±30 minutes — veto window",
                    {"blockers": blockers},
                )
            ]
        score = 50.0 + max(-40.0, min(40.0, sentiment * 10.0))
        return [
            EngineVerdict(
                "news", max(0.0, min(100.0, score)),
                f"sentiment score {sentiment:+.1f} over {len(items)} items",
                {"items": len(items), "sentiment": round(sentiment, 2)},
            )
        ]


class MathEngine:
    """Position sizing + stop geometry from the aggression dial (0-100).

    Pure math over the account snapshot; the actual sizing call goes through
    the bridge's `calculate_position_size` command for broker-truth
    tick values — this engine computes the *targets*.
    """

    name = "math"

    @staticmethod
    def risk_fraction(aggression: int) -> float:
        """0 → 0.25% of equity, 100 → 4.75% (never above the 5% contract cap)."""
        a = max(0, min(100, int(aggression)))
        return round(0.25 + a * 0.045, 3)

    @staticmethod
    def min_confidence(aggression: int) -> float:
        """Required router confidence: 95% at 0 aggression → 40% at 100."""
        a = max(0, min(100, int(aggression)))
        return round(95.0 - a * 0.55, 1)

    @staticmethod
    def stop_atr_multiplier(aggression: int) -> float:
        """Tighter stops as aggression rises: 2.2×ATR at 0 → 1.0×ATR at 100."""
        a = max(0, min(100, int(aggression)))
        return round(2.2 - a * 0.012, 2)

    @staticmethod
    def reward_atr_multiplier(aggression: int) -> float:
        """Reward target: 2.6×ATR at 0 (sniper) → 1.6×ATR at 100 (scalper)."""
        a = max(0, min(100, int(aggression)))
        return round(2.6 - a * 0.01, 2)

    def evaluate(self, snap: MarketSnapshot, aggression: int) -> EngineVerdict:
        # margin + exposure sanity
        score = 100.0
        reasons: list[str] = []
        if snap.margin_level is not None and snap.margin_level < 250:
            score -= 25
            reasons.append(f"margin level {snap.margin_level:.0f}% is tight")
        if snap.open_positions >= 6:
            score -= 20
            reasons.append(f"{snap.open_positions} open positions — exposure heavy")
        daily_dd = abs(min(0.0, snap.daily_pnl)) / max(1.0, snap.balance) * 100.0
        if daily_dd > 3.0:
            score -= min(40.0, daily_dd * 10.0)
            reasons.append(f"daily drawdown {daily_dd:.1f}% — size down")
        return EngineVerdict(
            "math",
            max(0.0, min(100.0, score)),
            "; ".join(reasons) or "risk math clear for this aggression level",
            {
                "risk_pct": self.risk_fraction(aggression),
                "min_confidence": self.min_confidence(aggression),
                "sl_atr_mult": self.stop_atr_multiplier(aggression),
                "tp_atr_mult": self.reward_atr_multiplier(aggression),
                "daily_drawdown_pct": round(daily_dd, 2),
            },
        )

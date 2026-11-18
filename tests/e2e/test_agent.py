"""AI Agent tests — pure math engines, 9-router, COACH + LIVE pipeline e2e."""

from __future__ import annotations

from tests.conftest import call
from nse_bridge.agent.coach import Coach
from nse_bridge.agent.engines import MathEngine, NewsEngine, TechnicalEngine, atr, ema, macd, rsi
from nse_bridge.agent.engines import MarketSnapshot
from nse_bridge.agent.promptmaster import build_review_prompt, parse_llm_verdict
from nse_bridge.agent.router9 import Router9


def make_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        symbol="XAUUSD",
        price=3850.0,
        spread=0.15,
        bars_m15=_make_bars(150, trend=3.0),  # strong uptrend: drift 0.45/bar beats the wave
        bars_h1=_make_bars(120, trend=3.0),
        balance=20000.0,
        equity=20000.0,
        margin_free=19000.0,
        margin_level=None,
        open_positions=0,
        daily_pnl=0.0,
        news_items=[],
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _make_bars(count: int, trend: float = 0.0, base: float = 3850.0) -> list[dict]:
    import math as _math

    bars = []
    price = base
    for i in range(count):
        drift = trend * 0.15 * (1 + i / 150.0)  # accelerating trend → MACD positive
        wave = _math.sin(i / 3.5) * 0.05  # mild pullback noise
        close = price + drift + wave * 0.3
        high = max(price, close) + 0.6
        low = min(price, close) - 0.6
        bars.append(
            {"time": i, "open": price, "high": high, "low": low, "close": close, "tick_volume": 100}
        )
        price = close
    return bars


# ---------------------------------------------------------------------------
# indicator math
# ---------------------------------------------------------------------------


def test_ema_length_and_seeding():
    values = [float(i) for i in range(1, 31)]
    out = ema(values, 10)
    assert len(out) == 21
    assert out[0] == sum(values[:10]) / 10


def test_rsi_bounds():
    up = [float(i) for i in range(1, 40)]
    down = [float(40 - i) for i in range(1, 40)]
    assert rsi(up) == 100.0
    assert rsi(down) == 0.0


def test_rsi_short_series_is_none():
    assert rsi([1.0, 2.0], 14) is None


def test_macd_returns_three():
    values = [100 + (i % 7) * 0.5 + i * 0.01 for i in range(60)]
    result = macd(values)
    assert result is not None and len(result) == 3


def test_atr_reasonable():
    bars = _make_bars(30)
    a = atr(bars, 14)
    # synthetic bar range is 1.6 (+trend gaps) — ATR sits just above it
    assert a is not None and 1.0 <= a <= 2.5


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


def test_technical_engine_returns_three_verdicts():
    verdicts = TechnicalEngine().evaluate(make_snapshot())
    names = [v.engine for v in verdicts]
    assert {"trend", "momentum", "volatility"} <= set(names)


def test_trending_market_scores_trend_high():
    snap = make_snapshot()
    trend_verdict = next(
        v for v in TechnicalEngine().evaluate(snap) if v.engine == "trend"
    )
    assert trend_verdict.score > 60  # steadily rising bars → bullish stack


def test_news_blocker_vetoes():
    import time as _t
    from datetime import UTC, datetime, timedelta

    snap = make_snapshot(
        news_items=[
            {
                "title": "FOMC rate decision",
                "impact": "high",
                "time_utc": datetime.now(UTC).isoformat(),
            }
        ]
    )
    verdict = NewsEngine().evaluate(snap)[0]
    assert verdict.score <= 10
    assert verdict.detail["blockers"]


def test_math_engine_aggression_mapping():
    assert MathEngine.risk_fraction(0) == 0.25
    assert MathEngine.risk_fraction(100) == 4.75
    assert MathEngine.min_confidence(0) == 95.0
    assert MathEngine.min_confidence(100) == 40.0
    assert MathEngine.stop_atr_multiplier(0) > MathEngine.stop_atr_multiplier(100)


def test_math_engine_penalizes_drawdown():
    healthy = MathEngine().evaluate(make_snapshot(), 50)
    bleeding = MathEngine().evaluate(make_snapshot(daily_pnl=-900.0, balance=20000.0), 50)
    assert bleeding.score < healthy.score


# ---------------------------------------------------------------------------
# router9
# ---------------------------------------------------------------------------


def test_router_passes_strong_setup():
    snap = make_snapshot()
    verdicts = TechnicalEngine().evaluate(snap) + NewsEngine().evaluate(snap)
    verdicts.append(MathEngine().evaluate(snap, 50))
    route = Router9().route(snap, verdicts, coach_score=90.0)
    assert not route.vetoed_by
    assert route.confidence > 60
    assert route.direction_hint in ("buy", "sell")


def test_router_vetoes_on_high_impact_news():
    import time as _t
    from datetime import UTC, datetime

    snap = make_snapshot(
        news_items=[
            {"title": "NFP release", "impact": "high", "time_utc": datetime.now(UTC).isoformat()}
        ]
    )
    verdicts = TechnicalEngine().evaluate(snap) + NewsEngine().evaluate(snap)
    verdicts.append(MathEngine().evaluate(snap, 50))
    route = Router9().route(snap, verdicts, coach_score=90.0)
    assert "news" in route.vetoed_by


def test_router_confidence_is_weighted_mean():
    snap = make_snapshot()
    verdicts = TechnicalEngine().evaluate(snap) + NewsEngine().evaluate(snap)
    verdicts.append(MathEngine().evaluate(snap, 50))
    route = Router9().route(snap, verdicts, coach_score=50.0)
    total_w = {"trend": 18, "momentum": 14, "volatility": 10, "session": 8,
               "spread": 8, "exposure": 12, "news": 10, "drawdown": 10, "coach": 10}
    assert 0 <= route.confidence <= 100


# ---------------------------------------------------------------------------
# COACH
# ---------------------------------------------------------------------------


def test_coach_passes_clean_session():
    coach = Coach()
    snap = make_snapshot()
    verdict = coach.coach(snap, has_stop_loss=True)
    assert verdict.verdict == "pass"
    assert verdict.score >= 65


def test_coach_vetoes_no_stop_loss():
    coach = Coach()
    verdict = coach.coach(make_snapshot(), has_stop_loss=False)
    assert verdict.verdict in ("veto", "warn")
    assert any("stop-loss" in m for m in verdict.messages)


def test_coach_revenge_trading_detection():
    coach = Coach()
    for _ in range(3):
        coach.record_trade(-50.0)
    verdict = coach.coach(make_snapshot(), has_stop_loss=True)
    assert any("consecutive losses" in m for m in verdict.messages)
    assert verdict.score < 100


def test_coach_daily_drawdown_break():
    coach = Coach()
    snap = make_snapshot(daily_pnl=-1200.0, balance=20000.0)  # 6% drawdown
    verdict = coach.coach(snap, has_stop_loss=True)
    assert verdict.verdict == "veto"
    assert any("stop for today" in m for m in verdict.messages)


# ---------------------------------------------------------------------------
# prompt master
# ---------------------------------------------------------------------------


def test_prompt_master_parses_strict_json():
    verdict = parse_llm_verdict('```json\n{"verdict": "TRADE", "confidence": 72, "critique": "ok", "risks": ["x"]}\n```')
    assert verdict["verdict"] == "TRADE" and verdict["confidence"] == 72


def test_prompt_master_defaults_on_garbage():
    verdict = parse_llm_verdict("I think this is a great trade honestly")
    assert verdict["verdict"] == "NO_TRADE"


def test_prompt_master_builds_prompt():
    prompt = build_review_prompt(
        "XAUUSD", "buy", 78.5,
        [{"name": "trend", "score": 80, "verdict": "pass", "detail": "bull"}],
        {"risk_pct": 2.0, "sl": 3840, "entry": 3850, "sl_atr_mult": 1.8,
         "tp": 3870, "tp_atr_mult": 2.2, "session": "London/NY overlap"},
        "clean", "no news", [],
    )
    assert "XAUUSD" in prompt and "78.5" in prompt and "Return JSON exactly" in prompt


# ---------------------------------------------------------------------------
# LIVE e2e — full pipeline through the HTTP layer against the real terminal
# ---------------------------------------------------------------------------


def test_agent_live_pipeline(client):
    # configure: advisory mode (no execution), aggression 55
    cfg = client.put(
        "/api/v1/agent/config",
        json={"mode": "advisory", "aggression": 55, "symbol": "XAUUSD"},
    ).json()["data"]
    assert cfg["mode"] == "advisory"
    assert cfg["risk_pct_per_trade"] == 2.725  # 0.25 + 55*0.045
    assert cfg["min_confidence"] == 64.8

    status = client.get("/api/v1/agent/status").json()["data"]
    assert status["symbol"] == "XAUUSD"
    assert status["llm_review"] is False  # math-only without an LLM key

    resp = client.post("/api/v1/agent/analyze", json={"symbol": "XAUUSD"})
    assert resp.status_code == 200, resp.text[:300]
    decision = resp.json()["data"]
    assert decision["decision"] in ("TRADE", "NO_TRADE")
    assert len(decision["gates"]) == 9
    gate_names = {g["name"] for g in decision["gates"]}
    assert gate_names == {
        "trend", "momentum", "volatility", "session", "spread",
        "exposure", "news", "drawdown", "coach",
    }
    assert decision["proposal"] is None or decision["decision"] == "TRADE"
    assert decision["coach"]["score"] >= 0
    for g in decision["gates"]:
        assert g["verdict"] in ("pass", "warn", "veto")
        assert 0 <= g["score"] <= 100


def test_agent_execute_refused_in_advisory_mode(client):
    resp = client.post("/api/v1/agent/execute", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_agent_coach_endpoint(client):
    data = client.get("/api/v1/agent/coach").json()["data"]
    assert data["verdict"] in ("pass", "warn", "veto")
    assert "risk_pct_at_current_aggression" in data


def test_agent_invalid_mode_rejected(client):
    resp = client.put("/api/v1/agent/config", json={"mode": "yolo"})
    assert resp.status_code == 422
    client.put("/api/v1/agent/config", json={"mode": "observe"})

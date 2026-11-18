"""The Prompt Master — composes LLM prompts from engine output.

The engines are the source of truth; the LLM is a *reviewer and narrator*,
never the calculator. The prompt packs the compact engine evidence and
demands a strict JSON verdict back — parsed defensively, math-checked by
the router afterwards.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """You are the NSE trading coach reviewing a deterministic \
multi-engine analysis of a MetaTrader 5 symbol. The numbers are computed by \
the bridge's math engines (EMA/RSI/MACD/ATR/Bollinger, spread, session, \
exposure, drawdown, news sentiment). Your job: challenge the setup in one \
tight paragraph, flag what the rules may have missed, and return a strict \
JSON verdict — no prose outside the JSON, no invented numbers. If the \
evidence is weak, say NO_TRADE. Never output a volume or an SL/TP that \
contradicts the provided math; the sizing is the math engine's job."""

USER_TEMPLATE = """Analyze {symbol} ({direction} bias from trend+momentum routing).

ROUTER CONFIDENCE: {confidence:.1f}/100 (vetoed gates: {vetoed})
GATES:
{gates}

MATH: risk {risk_pct}% of equity, SL {sl} (entry {entry}, ATR mult {sl_mult}), \
TP {tp} (ATR mult {tp_mult}), session {session}.

NEWS: {news_summary}

COACH: {coach}

Return JSON exactly:
{{"verdict": "TRADE|NO_TRADE", "confidence": <0-100>, \
"critique": "<=2 sentences", "risks": ["..."]}}"""


def build_review_prompt(
    symbol: str,
    direction: str,
    confidence: float,
    gates: list[dict[str, Any]],
    math: dict[str, Any],
    coach: str,
    news_summary: str,
    vetoed: list[str],
) -> str:
    gate_lines = "\n".join(
        f"- {g['name']}: {g['score']:.0f} ({g['verdict']}) — {g['detail']}" for g in gates
    )
    return USER_TEMPLATE.format(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        vetoed=", ".join(vetoed) or "none",
        gates=gate_lines,
        risk_pct=math.get("risk_pct"),
        sl=math.get("sl"),
        entry=math.get("entry"),
        sl_mult=math.get("sl_atr_mult"),
        tp=math.get("tp"),
        tp_mult=math.get("tp_atr_mult"),
        session=math.get("session", "-"),
        news_summary=news_summary or "none",
        coach=coach,
    )


def parse_llm_verdict(text: str) -> dict[str, Any]:
    """Defensively parse the LLM verdict JSON out of whatever came back."""
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return {"verdict": "NO_TRADE", "confidence": 0.0, "critique": "unparseable LLM reply", "risks": []}
        payload = json.loads(text[start : end + 1])
        verdict = str(payload.get("verdict", "NO_TRADE")).upper()
        if verdict not in ("TRADE", "NO_TRADE"):
            verdict = "NO_TRADE"
        return {
            "verdict": verdict,
            "confidence": max(0.0, min(100.0, float(payload.get("confidence", 0)))),
            "critique": str(payload.get("critique", ""))[:500],
            "risks": [str(r) for r in payload.get("risks", [])][:8],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"verdict": "NO_TRADE", "confidence": 0.0, "critique": "unparseable LLM reply", "risks": []}

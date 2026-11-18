"""Optional LLM client (OpenAI-compatible /v1/chat/completions).

Without `llm_base_url` + `llm_api_key` configured, the agent runs in pure
math mode (deterministic engines + router + coach) — the LLM only reviews
and narrates when present.
"""

from __future__ import annotations

import httpx
from loguru import logger

from .promptmaster import SYSTEM_PROMPT, USER_TEMPLATE, parse_llm_verdict


class LlmClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 25.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg) -> "LlmClient | None":
        if not cfg.agent_llm_base_url or not cfg.agent_llm_api_key:
            return None
        return cls(cfg.agent_llm_base_url, cfg.agent_llm_api_key, cfg.agent_llm_model)

    def review(
        self,
        symbol: str,
        direction: str,
        confidence: float,
        gates: list[dict],
        math: dict,
        coach: str,
        news_summary: str,
        vetoed: list[str],
    ) -> dict | None:
        """Returns the parsed verdict dict, or None when unavailable/error."""
        user = USER_TEMPLATE.format(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            vetoed=", ".join(vetoed) or "none",
            gates="\n".join(
                f"- {g['name']}: {g['score']:.0f} ({g['verdict']}) — {g['detail']}" for g in gates
            ),
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
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 400,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return parse_llm_verdict(content)
        except Exception as exc:  # noqa: BLE001 — the LLM is advisory only
            logger.warning("[AGENT] LLM review failed: {}", exc)
            return None

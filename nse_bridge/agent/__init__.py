"""NSE AI Agent — multi-engine analysis, 9-router decision gate, COACH system.

Architecture (all deterministic engines first, LLM optional):
    engines.py    — TechnicalEngine / NewsEngine / MathEngine (pure math)
    router9.py    — 9-gate decision router, each gate scores 0-100
    coach.py      — COACH system: rule-based discipline verdicts + lessons
    promptmaster.py — Prompt Master: composes LLM prompts from engine output
    llm.py        — optional OpenAI-compatible client (math-only without it)
    engine.py     — AgentService: analyze → route → decide → (execute)
"""

from .engine import AgentService

__all__ = ["AgentService"]

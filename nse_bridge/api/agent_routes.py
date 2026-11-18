"""AI Agent REST routes (§AI Agent tab): analyze, config, execute, coach.

The agent runs client-side over the bridge's own command surface; the
platform mirrors every decision into its audit trail when connected.
"""

from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..agent.engine import MODES
from ..agent.engines import MathEngine, MarketSnapshot

router = APIRouter(prefix="/api/v1/agent", tags=["ai-agent"])


class AgentConfigRequest(BaseModel):
    mode: str | None = Field(None, description=f"one of {MODES}")
    aggression: int | None = Field(None, ge=0, le=100, description="0 = sniper, 100 = degen")
    symbol: str | None = None
    llm_base_url: str | None = Field(None, description="OpenAI-compatible /v1 base; empty keeps current")
    llm_api_key: str | None = Field(None, description="empty/absent keeps the current key")
    llm_model: str | None = None
    news_items: list[dict[str, Any]] | None = Field(
        None, description="news items for the sentiment engine: {title, impact, time_utc}"
    )
    auto_execute: bool | None = Field(
        None, description="explicit consent for mode=auto to dispatch orders"
    )


class AnalyzeRequest(BaseModel):
    symbol: str | None = Field(None, description="default: the configured agent symbol")


@router.post("/analyze", summary="Run the multi-engine AI Agent analysis")
async def agent_analyze(request: Request, body: AnalyzeRequest | None = None) -> dict:
    agent = request.app.state.ctx.agent
    symbol = (body.symbol if body else None) or agent.config.symbol
    decision = await anyio.to_thread.run_sync(agent.analyze, symbol)
    return {"ok": True, "data": decision}


@router.get("/status", summary="AI Agent status")
async def agent_status(request: Request) -> dict:
    agent = request.app.state.ctx.agent
    return {"ok": True, "data": agent.status()}


@router.put("/config", summary="Configure the AI Agent (mode/aggression/LLM/news)")
async def agent_config(request: Request, body: AgentConfigRequest) -> dict:
    agent = request.app.state.ctx.agent
    data = agent.configure(
        mode=body.mode,
        aggression=body.aggression,
        symbol=body.symbol,
        llm_base_url=body.llm_base_url,
        llm_api_key=body.llm_api_key,
        llm_model=body.llm_model,
        news_items=body.news_items,
    )
    if body.auto_execute is not None:
        agent.config.auto_execute = body.auto_execute
    return {"ok": True, "data": data}


@router.post("/execute", summary="Execute the last approved proposal (mode=auto only)")
async def agent_execute(request: Request) -> dict:
    agent = request.app.state.ctx.agent
    result = await anyio.to_thread.run_sync(agent.execute)
    return {"ok": True, "data": result}


@router.get("/coach", summary="COACH verdict for the current session")
async def agent_coach(request: Request) -> dict:
    agent = request.app.state.ctx.agent

    def gather() -> MarketSnapshot:
        from ..engine.common import account_info_model
        from ..engine.positions import PositionsRequest, get_trading_open_positions

        acc = account_info_model(agent.ctx)
        positions = get_trading_open_positions(agent.ctx, PositionsRequest())
        daily_pnl = sum(float(p.get("profit") or 0.0) for p in positions["positions"])
        return MarketSnapshot(
            symbol=agent.config.symbol,
            price=0.0,
            spread=0.0,
            bars_m15=[],
            bars_h1=[],
            balance=float(acc.balance or 0.0),
            equity=float(acc.equity or 0.0),
            margin_free=float(acc.margin_free or 0.0),
            margin_level=float(acc.margin_level) if acc.margin_level else None,
            open_positions=positions["count"],
            daily_pnl=daily_pnl,
        )

    snap = await anyio.to_thread.run_sync(gather)
    verdict = agent.coach.coach(
        snap, has_stop_loss=True, proposals_today=agent._executed_today
    )
    return {
        "ok": True,
        "data": {
            "score": round(verdict.score, 1),
            "verdict": verdict.verdict,
            "messages": verdict.messages,
            "stats": verdict.stats,
            "risk_pct_at_current_aggression": MathEngine.risk_fraction(
                agent.config.aggression
            ),
        },
    }

"""Category 2 — trading account commands (10-18)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .common import BridgeContext, account_info_model
from .registry import command


class BalanceResponse(BaseModel):
    balance: float
    currency: str | None = None
    login: int


class EquityResponse(BaseModel):
    equity: float
    currency: str | None = None
    login: int


class MarginResponse(BaseModel):
    margin: float
    currency: str | None = None


class FreeMarginResponse(BaseModel):
    margin_free: float
    currency: str | None = None


class MarginLevelResponse(BaseModel):
    margin_level: float | None
    currency: str | None = None
    note: str | None = None


class ProfitResponse(BaseModel):
    profit: float
    currency: str | None = None


class CurrencyResponse(BaseModel):
    currency: str
    login: int


class TradingPermissionsResponse(BaseModel):
    trade_allowed: bool | None = None
    trade_expert: bool | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    terminal_trade_allowed: bool | None = None
    #: Account margin mode (MT5 ``margin_mode``): 0 netting, 1 hedging,
    #: 2 exchange. NOT the stop-out mode (``margin_so_mode``) — an
    #: earlier build returned the stop-out mode here (fixed in C2).
    margin_mode: int | None = None
    margin_mode_label: str | None = None
    account_login: int


@command(
    "get_trading_account_info",
    number=10,
    category="account",
    title="Get Full Trading Account Info",
    description="Complete account snapshot: login, balance, equity, margins, leverage, currency, trade mode.",
    response_model=object,
)
def get_trading_account_info(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"account": acc.model_dump()}


@command(
    "get_account_balance",
    number=11,
    category="account",
    title="Get Account Balance",
    description="Current account balance in the account currency.",
    response_model=BalanceResponse,
)
def get_account_balance(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"balance": float(acc.balance or 0.0), "currency": acc.currency, "login": acc.login}


@command(
    "get_account_equity",
    number=12,
    category="account",
    title="Get Account Equity",
    description="Live equity (balance + floating PnL).",
    response_model=EquityResponse,
)
def get_account_equity(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"equity": float(acc.equity or 0.0), "currency": acc.currency, "login": acc.login}


@command(
    "get_account_margin",
    number=13,
    category="account",
    title="Get Used Margin",
    description="Margin currently locked by open positions.",
    response_model=MarginResponse,
)
def get_account_margin(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"margin": float(acc.margin or 0.0), "currency": acc.currency}


@command(
    "get_account_free_margin",
    number=14,
    category="account",
    title="Get Free Margin",
    description="Margin available for new trades.",
    response_model=FreeMarginResponse,
)
def get_account_free_margin(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"margin_free": float(acc.margin_free or 0.0), "currency": acc.currency}


@command(
    "get_account_margin_level",
    number=15,
    category="account",
    title="Get Margin Level",
    description="Margin level in percent (equity/margin*100). None when no positions are open.",
    response_model=MarginLevelResponse,
)
def get_account_margin_level(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    level = acc.margin_level
    note = None
    if not level:
        note = "No open positions — margin level undefined (infinite)."
    return {"margin_level": float(level) if level else None, "currency": acc.currency, "note": note}


@command(
    "get_account_profit",
    number=16,
    category="account",
    title="Get Floating Profit",
    description="Current floating profit/loss of all open positions.",
    response_model=ProfitResponse,
)
def get_account_profit(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"profit": float(acc.profit or 0.0), "currency": acc.currency}


@command(
    "get_account_currency",
    number=17,
    category="account",
    title="Get Account Currency",
    description="Base currency of the account.",
    response_model=CurrencyResponse,
)
def get_account_currency(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    return {"currency": acc.currency or "USD", "login": acc.login}


@command(
    "get_trading_permissions",
    number=18,
    category="account",
    title="Get Trading Permissions",
    description="Account + terminal trading permissions (EA trading enabled, trade mode, margin call levels).",
    response_model=TradingPermissionsResponse,
)
def get_trading_permissions(ctx: BridgeContext, params: None) -> dict[str, Any]:
    acc = account_info_model(ctx)
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.terminal_info()

    raw = ctx.driver.run(job, op="terminal_info")
    return {
        "trade_allowed": acc.trade_allowed,
        "trade_expert": acc.trade_expert,
        "trade_mode": acc.trade_mode,
        "trade_mode_label": acc.trade_mode_label,
        "terminal_trade_allowed": bool(raw.trade_allowed) if raw else None,
        "margin_mode": acc.margin_mode,
        "margin_mode_label": acc.margin_mode_label,
        "account_login": acc.login,
    }

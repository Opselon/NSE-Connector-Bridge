"""Command registry: the 91 NSE MT5 Bridge commands.

Each command = CommandSpec(name, category, handler, request/response models,
mutates flag). The FastAPI layer auto-generates one POST endpoint per command
plus Swagger docs from these specs; the WebSocket server channel enforces
this registry as the command allowlist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass
class CommandSpec:
    name: str
    category: str
    title: str
    description: str
    handler: Callable[..., Any]
    request_model: type[BaseModel] | None = None
    # Either a BaseModel subclass, or the `object` sentinel meaning
    # "untyped dict response" (api/app.py checks `rm is not object`).
    # `type[Any]` is the honest type: `type[object]` would collapse the
    # union and lose the BaseModel intent for readers.
    response_model: type[Any] | None = None
    mutates: bool = False  # dispatches broker writes (trades)
    number: int = 0  # official command number (1..90 originally; 91 added by the
    # complete-infrastructure spec §6, close-by — see trade_close_position_by)


REGISTRY: list[CommandSpec] = []
CATEGORY_ORDER = (
    "system",
    "connection",
    "account",
    "symbols",
    "market",
    "charts",
    "positions",
    "orders",
    "trading",
    "risk",
    "diagnostics",
)


def command(
    name: str,
    *,
    number: int,
    category: str,
    title: str,
    description: str,
    request_model: type[BaseModel] | None = None,
    response_model: type[Any] | None = None,
    mutates: bool = False,
) -> Callable:
    def deco(fn: Callable[..., Any]) -> Callable:
        REGISTRY.append(
            CommandSpec(
                name=name,
                category=category,
                title=title,
                description=description,
                handler=fn,
                request_model=request_model,
                response_model=response_model,
                mutates=mutates,
                number=number,
            )
        )
        return fn

    return deco


def all_commands() -> list[CommandSpec]:
    return sorted(REGISTRY, key=lambda c: c.number)


def commands_by_category(category: str) -> list[CommandSpec]:
    return [c for c in REGISTRY if c.category == category]


def find(name: str) -> CommandSpec | None:
    for c in REGISTRY:
        if c.name == name:
            return c
    return None

"""Unit tests for the local 91-command registry (Linux-runnable, no MT5).

Importing `nse_bridge.engine` registers every command via decorators.
The registry is the Bridge's LOCAL capability set: all 91 commands stay
available locally even though the server channel (contract §6.1) only
ever sends 3 of them remotely. Command 91 is `trade_close_position_by`
(spec §6 close-by, added in C2); the count is pinned so a silently
dropped or duplicated registration fails loudly.
"""

from __future__ import annotations

import pytest

import nse_bridge.engine  # noqa: F401  (importing registers all commands)
from nse_bridge.engine.registry import CATEGORY_ORDER, REGISTRY, all_commands, find

pytestmark = pytest.mark.unit


def test_registry_has_exactly_91_commands():
    assert len(all_commands()) == 91
    assert len(REGISTRY) == 91


def test_command_numbers_are_unique_and_cover_1_to_91():
    numbers = [c.number for c in all_commands()]
    assert sorted(numbers) == list(range(1, 92))
    assert len(set(numbers)) == 91


def test_command_names_are_unique():
    names = [c.name for c in all_commands()]
    assert len(set(names)) == 91
    assert all(isinstance(n, str) and n for n in names)


def test_all_commands_have_a_callable_handler_and_known_category():
    for cmd in all_commands():
        assert callable(cmd.handler), cmd.name
        assert cmd.category in CATEGORY_ORDER, cmd.name
        assert cmd.title, cmd.name
        assert cmd.description, cmd.name


def test_all_commands_sorted_by_number():
    numbers = [c.number for c in all_commands()]
    assert numbers == sorted(numbers)


def test_find_resolves_known_commands_and_returns_none_for_unknown():
    assert find("get_trading_account_info") is not None
    assert find("connect_mt5") is not None
    assert find("calculate_position_size") is not None  # risk calc 73-81 stay local
    assert find("trade_close_position_by") is not None  # close-by, command 91 (C2)
    assert find("does_not_exist") is None


def test_risk_calculator_commands_present_locally():
    # Commands 73-81 are the local risk calculators. They stay in the
    # LOCAL registry (used for local validation); the server channel
    # excludes them via the contract §6.1 allowlist instead.
    risk = [c for c in all_commands() if c.category == "risk"]
    assert len(risk) == 9
    assert sorted(c.number for c in risk) == list(range(73, 82))


def test_every_category_in_registry():
    present = {c.category for c in all_commands()}
    assert present == set(CATEGORY_ORDER)

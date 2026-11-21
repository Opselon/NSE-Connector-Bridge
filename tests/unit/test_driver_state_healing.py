"""Driver state-machine tests: the terminal liveness probe is symmetric.

A single transient `terminal_info().connected == False` used to
 degrade the driver state to TERMINAL_ERROR permanently — nothing
restored CONNECTED short of a manual reconnect, so the UI showed
TERMINAL_ERROR while the account was demonstrably connected (observed
2026-11-21, terminal build 6327, live on the user's machine). The
liveness probe must HEAL: a probe proving the link is up restores
CONNECTED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from nse_bridge.config import BridgeSettings
from nse_bridge.mt5.driver import Mt5Driver


def _driver(tmp_path: Path) -> Mt5Driver:
    settings = BridgeSettings(data_dir=tmp_path)
    return Mt5Driver(settings)


def _patch_probe(monkeypatch, driver: Mt5Driver, connected: bool) -> None:
    monkeypatch.setattr(
        driver,
        "run",
        lambda job, op=None, **_k: {
            "connected": connected,
            "name": "MetaTrader 5",
            "trade_allowed": True,
            "login": 10011755849,
        },
    )


def test_refresh_terminal_degrades_on_disconnected_probe(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    driver._connected = True
    driver._set_state("CONNECTED")
    _patch_probe(monkeypatch, driver, connected=False)
    info = driver.refresh_terminal()
    assert info["connected"] is False
    assert driver._state == "TERMINAL_ERROR"


def test_refresh_terminal_heals_transient_error_state(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    driver._connected = True
    driver._set_state("TERMINAL_ERROR", "terminal reports disconnected")
    _patch_probe(monkeypatch, driver, connected=True)
    info = driver.refresh_terminal()
    assert info["connected"] is True
    assert driver._state == "CONNECTED", "a good probe must restore CONNECTED"
    assert "recovered" in driver._state_detail


def test_refresh_terminal_keeps_connected_state(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    driver._connected = True
    driver._set_state("CONNECTED")
    _patch_probe(monkeypatch, driver, connected=True)
    driver.refresh_terminal()
    assert driver._state == "CONNECTED"
    # No spurious state churn: the detail must not be rewritten on a
    # healthy probe (the original connect detail stays).
    assert "recovered" not in driver._state_detail

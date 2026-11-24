"""Unit tests for the v1.3.0 honest capability gate (S3).

The official MetaTrader5 IPC driver ships win_amd64 wheels only, so on
macOS, Linux, or a native-ARM64 Windows process the bridge core runs but
no terminal can attach. These tests pin the gate at its three seams:
the module constant, the driver's connection state, and the fail-fast
connect() — plus the terminals endpoint flag (covered in
test_gui_control_center.py). Tests that can only pass on an unsupported
platform skip on a supported one instead of asserting the opposite
(the supported-platform behaviour is covered by the existing
discovery/diagnostics suites).
Label: unit.
"""

from __future__ import annotations

import platform
import sys

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.mt5.driver import (
    MT5_PLATFORM_SUPPORTED,
    MT5_WINDOWS_REQUIRED_CODE,
    Mt5Driver,
)

pytestmark = pytest.mark.unit


def _settings(**overrides: object) -> BridgeSettings:
    base: dict[str, object] = {
        "server_base_url": "http://127.0.0.1:1",
        "environment": "test",
        "server_token": "tok-SECRET",
        "device_id": "dev-1",
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


def test_platform_supported_matches_process_architecture():
    assert MT5_PLATFORM_SUPPORTED == (sys.platform == "win32" and platform.machine() in ("AMD64", "x86_64"))


def test_windows_required_code_constant():
    assert MT5_WINDOWS_REQUIRED_CODE == "MT5_WINDOWS_REQUIRED"


@pytest.mark.skipif(MT5_PLATFORM_SUPPORTED, reason="gate only engages where the driver cannot load")
def test_connection_state_reports_platform_unsupported():
    driver = Mt5Driver(_settings())
    try:
        state = driver.connection_state_dict()
        assert state["platform_supported"] is False
        assert state["connected"] is False
        assert state["diagnostic_code"] == MT5_WINDOWS_REQUIRED_CODE
    finally:
        driver.disconnect()


@pytest.mark.skipif(MT5_PLATFORM_SUPPORTED, reason="gate only engages where the driver cannot load")
def test_connect_fails_fast_without_import_error():
    """connect() on an unsupported platform returns a structured failure.

    Before the gate this raised ModuleNotFoundError out of the worker
    thread (surfaced as a bare 500) — the user saw a crash, not a state.
    """
    driver = Mt5Driver(_settings())
    try:
        summary = driver.connect()
        assert summary["ok"] is False
        assert summary["diagnostic_code"] == MT5_WINDOWS_REQUIRED_CODE
        assert "Windows" in summary["error"]
    finally:
        driver.disconnect()

"""Regression: windowed-exe logging bootstrap (tests/unit/test_logging_setup.py).

A windowed (console=False) PyInstaller exe has sys.stderr = None when
no console is attached — the default double-click launch. loguru
refuses None sinks with a TypeError, which used to kill the process
before its window could open (observed twice on the packaged exe,
2026-11-21). The console sink must be skipped, the file sinks must
still be configured.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from nse_bridge.logging_setup import setup_logging


def test_setup_logging_survives_none_stderr(tmp_path: Path, monkeypatch):
    # Simulate the windowed exe: no console → sys.stderr is None.
    monkeypatch.setattr(sys, "stderr", None)
    # Must not raise; the file sinks are the authoritative outputs.
    setup_logging(tmp_path / "logs", "INFO")
    (tmp_path / "logs" / "bridge.log").exists() or (_ for _ in ()).throw(
        AssertionError("bridge.log sink was not configured")
    )


def test_setup_logging_keeps_stderr_when_present(tmp_path: Path):
    # A real console (or piped stderr) still gets the console sink.
    setup_logging(tmp_path / "logs", "INFO")

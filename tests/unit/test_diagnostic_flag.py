"""Phase 6 — the --diagnostic console flag (bridge.spec console=False).

The flag must parse (proved here without booting the bridge) and map
onto ``settings.diagnostic_mode`` in main(); the wiring is the three
lines in :func:`nse_bridge.main.main` guarded by ``args.diagnostic``.
The console allocation itself (``_enable_diagnostic_console``) is
Windows-frozen-only and best-effort, so it is covered by inspection,
not by a Linux test: on this platform it must be a silent no-op.
"""

from __future__ import annotations

import pytest

from nse_bridge.main import _enable_diagnostic_console, build_parser

pytestmark = pytest.mark.unit


def test_diagnostic_flag_defaults_off():
    args = build_parser().parse_args([])
    assert args.diagnostic is False


def test_diagnostic_flag_parses():
    args = build_parser().parse_args(["--diagnostic"])
    assert args.diagnostic is True


def test_diagnostic_flag_combines_with_other_flags():
    args = build_parser().parse_args(["--diagnostic", "--port", "8799", "--gui", "none"])
    assert args.diagnostic is True
    assert args.port == 8799
    assert args.gui == "none"


def test_diagnostic_console_helper_is_a_noop_off_windows():
    # Must not raise on Linux/dev runs (no console to allocate).
    _enable_diagnostic_console()

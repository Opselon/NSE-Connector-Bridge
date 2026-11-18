"""Unit tests for the --host guardrail (Phase 1 — security).

A non-loopback bind without the explicit allow_non_loopback flag must
be refused; loopback (and authorized non-loopback) binds are fine.
The --host flag itself is KEPT. Label: unit.
"""

from __future__ import annotations

import os

import pytest

from nse_bridge.config import is_loopback_host, validate_bind_host

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.1.2.3", "::1", "localhost"])
def test_loopback_hosts_allowed_without_flag(host):
    assert is_loopback_host(host) is True
    validate_bind_host(host, allow_non_loopback=False)  # no raise


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", ""])
def test_non_loopback_refused_without_authorization_flag(host):
    assert is_loopback_host(host) is False
    with pytest.raises(ValueError, match="refusing non-loopback bind"):
        validate_bind_host(host, allow_non_loopback=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_loopback_allowed_with_explicit_flag(host):
    validate_bind_host(host, allow_non_loopback=True)  # no raise


def test_main_refuses_non_loopback_host(tmp_path, monkeypatch):
    """main() with --host 0.0.0.0 and no flag exits before serving."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    from nse_bridge.main import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--host", "0.0.0.0", "--gui", "none"])
    assert excinfo.value.code == 2

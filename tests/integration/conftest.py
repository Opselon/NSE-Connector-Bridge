"""Integration-test fixtures: the CONTRACT v1 mock server lives in
``scripts/mock_server.py`` (importable, in-process)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from mock_server import MockServer


@pytest.fixture()
def mock_server():
    server = MockServer(token="test-token")
    server.start()
    yield server
    server.stop()

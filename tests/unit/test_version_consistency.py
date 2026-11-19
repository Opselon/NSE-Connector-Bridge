"""Version consistency: one release version, everywhere.

``nse_bridge.BRIDGE_VERSION`` is the single source of truth (the
installer, the version resource, and the release manifest all derive
from it via ``scripts/write_version_info.py`` /
``scripts/write_release_manifest.py``). The pyproject metadata and the
packaged docs must not drift from it again — v1.1.0 shipped with
``pyproject`` still claiming 1.0.0 (found in the Phase 2b audit).
"""

import tomllib
from pathlib import Path

import pytest

from nse_bridge import BRIDGE_VERSION

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_version_matches_bridge_version():
    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == BRIDGE_VERSION, (
        "pyproject [project].version drifted from nse_bridge.BRIDGE_VERSION "
        f"({BRIDGE_VERSION}) — bump both in the same release commit"
    )


def test_version_info_resource_matches_bridge_version():
    version_info = REPO_ROOT / "version_info.txt"
    assert version_info.exists(), "version_info.txt (Windows version resource) is missing"
    content = version_info.read_text(encoding="utf-8")
    assert BRIDGE_VERSION in content, (
        "version_info.txt does not carry BRIDGE_VERSION "
        f"({BRIDGE_VERSION}) — regenerate with scripts/write_version_info.py"
    )

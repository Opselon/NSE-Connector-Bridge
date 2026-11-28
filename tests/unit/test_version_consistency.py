"""Version consistency: one release version, everywhere.

``nse_bridge.BRIDGE_VERSION`` is the single source of truth (the
installer, the version resource, and the release manifest all derive
from it via ``scripts/write_version_info.py`` /
``scripts/write_release_manifest.py``). The pyproject metadata and the
packaged docs must not drift from it again — v1.1.0 shipped with
``pyproject`` still claiming 1.0.0 (found in the Phase 2b audit).

Tag consistency: no ``vX.Y.Z`` tag exists yet at the time a version
bump lands — tagging is a separate, gated step — so the tag check below
skips when no tag is present and pins ``tag == "v" + BRIDGE_VERSION``
when one is (CI sets ``GITHUB_REF_NAME`` on tag events; a local run
can pass ``BRIDGE_RELEASE_TAG`` instead).
"""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from nse_bridge import BRIDGE_VERSION

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "write_release_manifest.py"

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def test_bridge_version_is_plain_semver():
    assert SEMVER_RE.match(BRIDGE_VERSION), (
        f"BRIDGE_VERSION {BRIDGE_VERSION!r} is not a plain X.Y.Z semver string — "
        "release versions must be plain X.Y.Z so tags, the Windows version "
        "resource and the release manifest all derive from the same value"
    )


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


def test_version_info_filevers_matches_bridge_version():
    # Substring check above would pass for "1.4.1" inside "1.4.10"; the
    # numeric filevers tuple is the value the built exe actually reports.
    content = (REPO_ROOT / "version_info.txt").read_text(encoding="utf-8")
    match = re.search(r"filevers=\((\d+), (\d+), (\d+), (\d+)\)", content)
    assert match, "version_info.txt has no filevers=(major, minor, patch, build) tuple"
    major, minor, patch, _build = (int(g) for g in match.groups())
    assert (major, minor, patch) == tuple(int(p) for p in BRIDGE_VERSION.split(".")), (
        f"version_info.txt filevers {(major, minor, patch)} does not match "
        f"BRIDGE_VERSION {BRIDGE_VERSION} — regenerate with scripts/write_version_info.py"
    )


def test_windows_manifest_matches_bridge_version():
    manifest_path = REPO_ROOT / "windows" / "NSE-MT5-Bridge.exe.manifest"
    assert manifest_path.exists(), "windows/NSE-MT5-Bridge.exe.manifest is missing"
    content = manifest_path.read_text(encoding="utf-8")
    match = re.search(r'<assemblyIdentity version="(\d+)\.(\d+)\.(\d+)\.(\d+)"', content)
    assert match, "Windows manifest has no assemblyIdentity version"
    major, minor, patch, _build = (int(g) for g in match.groups())
    assert (major, minor, patch) == tuple(int(p) for p in BRIDGE_VERSION.split(".")), (
        f"Windows manifest version {(major, minor, patch)} does not match "
        f"BRIDGE_VERSION {BRIDGE_VERSION} — regenerate with scripts/write_version_info.py"
    )


def test_release_manifest_reports_bridge_version(tmp_path: Path):
    # write_release_manifest.py reads BRIDGE_VERSION into the manifest;
    # prove it end to end (dummy exe, no build, no network) so a release
    # can never ship a manifest that reports a different version than
    # the code it was built from.
    exe = tmp_path / "NSE-MT5-Bridge.exe"
    exe.write_bytes(b"fake-exe")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(MANIFEST_SCRIPT), str(exe), "--tag", f"v{BRIDGE_VERSION}", "--out-dir", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == BRIDGE_VERSION
    assert manifest["tag"] == f"v{BRIDGE_VERSION}"


def test_release_tag_matches_bridge_version_when_tag_present():
    # The vX.Y.Z tag does not exist yet when a bump lands (tagging is a
    # separately gated step), so this test skips without a tag — and pins
    # tag == "v" + BRIDGE_VERSION the moment one is present, so CI on
    # the tag event fails loudly instead of shipping a mislabelled exe.
    tag = os.environ.get("BRIDGE_RELEASE_TAG") or os.environ.get("GITHUB_REF_NAME")
    if not tag or not tag.startswith("v"):
        pytest.skip("no release tag present (GITHUB_REF_NAME / BRIDGE_RELEASE_TAG unset) — tag check is N/A")
    assert tag == f"v{BRIDGE_VERSION}", (
        f"release tag {tag} does not match BRIDGE_VERSION {BRIDGE_VERSION} — "
        "tag the release from the bumped version, or bump the version first"
    )

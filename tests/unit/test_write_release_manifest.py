"""Unit tests for scripts/write_release_manifest.py (v1.3.0 multi-platform).

The script is exercised as a subprocess against dummy files (no real
builds, no network): the single-exe shape stays backwards compatible
(scripts/install.ps1 reads files[].name/sha256), --extra adds per-file
platform entries + .sha256 sidecars, and a bad --extra fails loudly.
Label: unit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nse_bridge import BRIDGE_VERSION

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "write_release_manifest.py"


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_single_exe_shape_is_backwards_compatible(tmp_path: Path):
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", b"fake-exe")
    out = tmp_path / "out"
    proc = _run(str(exe), "--tag", "v1.3.1", "--out-dir", str(out))
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == BRIDGE_VERSION
    assert manifest["tag"] == "v1.3.1"
    assert manifest["platform"] == "windows-x64"
    assert manifest["platforms"] == ["windows-x64"]
    assert manifest["files"] == [
        {"name": exe.name, "sha256": _sha(exe), "size_bytes": exe.stat().st_size, "platform": "windows-x64"}
    ]
    sidecar = (out / "NSE-MT5-Bridge.exe.sha256").read_text(encoding="utf-8")
    assert sidecar == f"{_sha(exe)}  NSE-MT5-Bridge.exe\n"


def test_extra_artifacts_join_manifest_with_platforms(tmp_path: Path):
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", b"fake-exe")
    setup = _write(tmp_path / "NSE-MT5-Bridge-Setup-1.3.0.exe", b"fake-setup")
    dmg = _write(tmp_path / "NSE-MT5-Bridge-1.3.0-macos-arm64.dmg", b"fake-dmg")
    tgz = _write(tmp_path / "NSE-MT5-Bridge-1.3.0-linux-x64.tar.gz", b"fake-tgz")
    out = tmp_path / "out"
    proc = _run(
        str(exe),
        "--setup",
        str(setup),
        "--tag",
        "v1.3.0",
        "--out-dir",
        str(out),
        "--extra",
        f"macos-arm64:{dmg}",
        "--extra",
        f"linux-x64:{tgz}",
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["platforms"] == ["windows-x64", "macos-arm64", "linux-x64"]
    by_name = {f["name"]: f for f in manifest["files"]}
    assert by_name[dmg.name]["platform"] == "macos-arm64"
    assert by_name[dmg.name]["sha256"] == _sha(dmg)
    assert by_name[tgz.name]["platform"] == "linux-x64"
    assert by_name[setup.name]["platform"] == "windows-x64"
    # Every listed file has a matching .sha256 sidecar in the out dir.
    for entry in manifest["files"]:
        sidecar = (out / f"{entry['name']}.sha256").read_text(encoding="utf-8")
        assert sidecar == f"{entry['sha256']}  {entry['name']}\n"


def test_extra_with_missing_file_fails(tmp_path: Path):
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", b"fake-exe")
    proc = _run(str(exe), "--extra", "macos-arm64:/nonexistent/file.dmg")
    assert proc.returncode != 0
    assert "error" in proc.stderr.lower()


def test_extra_without_platform_separator_fails(tmp_path: Path):
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", b"fake-exe")
    proc = _run(str(exe), "--extra", str(exe))
    assert proc.returncode != 0

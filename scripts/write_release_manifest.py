"""Write the release manifest + SHA-256 checksum for a built exe.

Usage:
  python scripts/write_release_manifest.py <exe-path> [--out-dir <dir>]
      [--tag <tag>] [--commit <sha>] [--setup <setup-exe-path>]
      [--extra <platform>:<path> ...]

Outputs (next to the exe unless --out-dir is given):
  NSE-MT5-Bridge.exe.sha256   "<sha256>  NSE-MT5-Bridge.exe\\n"
  release-manifest.json       {name, version, tag, commit, platform,
                               platforms, built_from,
                               files: [{name, sha256, size_bytes, platform}]}
  <setup-exe>.sha256          only with --setup: "<sha256>  <setup-exe>\\n",
                               and the setup exe joins the manifest files
                               list (same shape as the exe entry).

Multi-platform releases (v1.3.0): the primary artifact is still the
Windows x64 exe (+ setup exe); every other platform artifact joins
via ``--extra <platform>:<path>`` (repeatable, e.g.
``--extra macos-arm64:dist/NSE-MT5-Bridge-1.3.0-macos-arm64.dmg``).
Each extra file gets its own ``.sha256`` sidecar in --out-dir and a
``platform`` field in the manifest's ``files`` list; the top-level
``platforms`` array lists every platform in the release. The
top-level ``platform`` field stays ``"windows-x64"`` (the primary /
trading platform) for backwards compatibility — scripts/install.ps1
only reads ``files[].name`` / ``files[].sha256`` and is unaffected.

The manifest is what a user verifies a download against: the SHA-256
in the manifest must match the .sha256 sidecar and the exe itself.
The script never invents a checksum — it hashes the real file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from nse_bridge import BRIDGE_VERSION


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — commit is informational; absence is not fatal
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path, help="built exe to checksum")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--tag", default=None, help="release tag, e.g. v1.0.0")
    parser.add_argument("--commit", default=None, help="commit SHA (default: git HEAD)")
    parser.add_argument(
        "--setup",
        type=Path,
        default=None,
        help="setup exe to checksum and list in the manifest, e.g. dist/NSE-MT5-Bridge-Setup-1.0.0.exe",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="PLATFORM:PATH",
        help="repeatable: an additional platform artifact to checksum and list, "
        "e.g. --extra macos-arm64:dist/NSE-MT5-Bridge-1.3.0-macos-arm64.dmg "
        "(each gets its own .sha256 sidecar and a platform field in the manifest)",
    )
    args = parser.parse_args(argv)

    exe = args.exe
    if not exe.is_file():
        print(f"error: exe not found: {exe}", file=sys.stderr)
        return 2
    setup = args.setup
    if setup is not None and not setup.is_file():
        print(f"error: setup exe not found: {setup}", file=sys.stderr)
        return 2
    extras: list[tuple[str, Path]] = []
    for spec in args.extra:
        platform_label, sep, raw_path = spec.partition(":")
        extra_path = Path(raw_path)
        if not sep or not platform_label or not extra_path.is_file():
            print(f"error: --extra must be PLATFORM:PATH to an existing file: {spec!r}", file=sys.stderr)
            return 2
        extras.append((platform_label, extra_path))
    out_dir = args.out_dir or exe.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    def _checksum_entry(path: Path) -> tuple[str, Path]:
        digest = sha256_of(path)
        sha_path = out_dir / f"{path.name}.sha256"
        sha_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        return digest, sha_path

    digest, sha_path = _checksum_entry(exe)

    files = [
        {"name": exe.name, "sha256": digest, "size_bytes": exe.stat().st_size, "platform": "windows-x64"},
    ]
    if setup is not None:
        setup_digest, setup_sha_path = _checksum_entry(setup)
        files.append(
            {"name": setup.name, "sha256": setup_digest, "size_bytes": setup.stat().st_size, "platform": "windows-x64"}
        )
    extras_written: list[tuple[str, Path, str]] = []
    for platform_label, extra_path in extras:
        extra_digest, extra_sha_path = _checksum_entry(extra_path)
        files.append(
            {
                "name": extra_path.name,
                "sha256": extra_digest,
                "size_bytes": extra_path.stat().st_size,
                "platform": platform_label,
            }
        )
        extras_written.append((platform_label, extra_sha_path, extra_digest))

    # One platform per file, in first-seen order; "platform" stays the
    # primary (Windows x64) for backwards compatibility.
    platforms = list(dict.fromkeys(f["platform"] for f in files))
    manifest = {
        "name": "NSE MT5 Bridge",
        "version": BRIDGE_VERSION,
        "tag": args.tag,
        "commit": args.commit or git_commit(),
        "platform": "windows-x64",
        "platforms": platforms,
        "files": files,
    }
    manifest_path = out_dir / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {sha_path} + {manifest_path} ({digest[:16]}…)")
    if setup is not None:
        print(f"wrote {setup_sha_path} ({setup_digest[:16]}…)")
    for platform_label, extra_sha_path, extra_digest in extras_written:
        print(f"wrote {extra_sha_path} ({extra_digest[:16]}…) [{platform_label}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

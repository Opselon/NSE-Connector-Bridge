"""Write the release manifest + SHA-256 checksum for a built exe.

Usage:
  python scripts/write_release_manifest.py <exe-path> [--out-dir <dir>]
      [--tag <tag>] [--commit <sha>]

Outputs (next to the exe unless --out-dir is given):
  NSE-MT5-Bridge.exe.sha256   "<sha256>  NSE-MT5-Bridge.exe\\n"
  release-manifest.json       {name, version, tag, commit, platform,
                               built_from, files: [{name, sha256, size_bytes}]}

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
    args = parser.parse_args(argv)

    exe = args.exe
    if not exe.is_file():
        print(f"error: exe not found: {exe}", file=sys.stderr)
        return 2
    out_dir = args.out_dir or exe.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    digest = sha256_of(exe)
    sha_path = out_dir / f"{exe.name}.sha256"
    sha_path.write_text(f"{digest}  {exe.name}\n", encoding="utf-8")

    manifest = {
        "name": "NSE MT5 Bridge",
        "version": BRIDGE_VERSION,
        "tag": args.tag,
        "commit": args.commit or git_commit(),
        "platform": "windows-x64",
        "files": [
            {"name": exe.name, "sha256": digest, "size_bytes": exe.stat().st_size},
        ],
    }
    manifest_path = out_dir / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {sha_path} + {manifest_path} ({digest[:16]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

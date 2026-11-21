"""Documentation validation (mission §I/§G): links, drift, OpenAPI freshness.

Runs in CI as the docs-only gate (PRs touching only docs previously
got NO signal at all — the expensive build jobs were skipped
entirely). Three checks, all factual:

1. Relative markdown links in README.md and every docs/*.md resolve
   to files that exist (a broken docs link ships to users; the Pages
   site is built from these files).
2. Command-count drift guard: the LIVE user surfaces (README.md,
   docs/index.html, docs/COMMAND_REFERENCE.md) must not claim a stale
   command count. The registry is parsed as the truth; any "N commands"
   / "N-command" claim on those surfaces must match it. Historical
   analysis documents (docs/GAP-ANALYSIS*.md) are exempt — they record
   dated findings, not current behavior.
3. OpenAPI freshness: docs/openapi.json must equal a fresh export
   (same guarantee the main CI gate enforces, kept here so the docs
   job stays meaningful on docs-only changes).

Usage:
  python scripts/check_docs.py
Exit 0 = clean; nonzero with a report of every violation.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MD_SOURCES = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
#: Surfaces whose claims must match current reality (not historical records).
DRIFT_SURFACES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "index.html",
    REPO_ROOT / "docs" / "COMMAND_REFERENCE.md",
]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
CLAIM_RE = re.compile(r"\b(\d{2,3})[- ](?:command|commands)\b", re.IGNORECASE)


def actual_command_count() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from nse_bridge.engine.registry import REGISTRY

    return len(REGISTRY)


def check_links() -> list[str]:
    problems: list[str] = []
    for source in MD_SOURCES:
        text = source.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            path_part = target.split("#")[0]
            if not path_part:
                continue  # same-page anchor
            resolved = (source.parent / path_part).resolve()
            if not resolved.exists():
                problems.append(f"{source.relative_to(REPO_ROOT)}: broken link -> {target}")
    return problems


def check_command_count_claims(count: int) -> list[str]:
    problems: list[str] = []
    for surface in DRIFT_SURFACES:
        if not surface.is_file():
            continue
        text = surface.read_text(encoding="utf-8")
        for match in CLAIM_RE.finditer(text):
            claimed = int(match.group(1))
            if claimed != count:
                problems.append(
                    f"{surface.relative_to(REPO_ROOT)}: claims {claimed} commands, "
                    f"registry has {count} (line: ...{text[max(0, match.start() - 40) : match.end() + 20]}...)"
                )
    return problems


def check_openapi_freshness() -> list[str]:
    problems: list[str] = []
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.export_openapi import export  # reuse the generator entry point

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        export(tmp_path)
        committed = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        fresh = json.loads(tmp_path.read_text(encoding="utf-8"))
        if committed != fresh:
            problems.append(
                "docs/openapi.json is stale — regenerate with: python scripts/export_openapi.py docs/openapi.json"
            )
    finally:
        tmp_path.unlink(missing_ok=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags yet; kept for script symmetry
    count = actual_command_count()
    problems = check_links() + check_command_count_claims(count) + check_openapi_freshness()
    print(f"command registry: {count} commands")
    if problems:
        print(f"docs check: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("docs check: clean (links, command-count claims, OpenAPI freshness)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

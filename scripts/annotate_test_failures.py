"""Post junit test failures as check-run annotations (CI observability).

When a test job fails, the junit XML is uploaded as an artifact — but
downloading artifacts and job logs needs GitHub auth, which a
coordinator without a token cannot use. This script runs inside the
job (where GITHUB_TOKEN exists) and posts each failing test as a
check-run annotation, which IS readable unauthenticated via the
Checks API (``GET .../check-runs/{id}/annotations``).

It never fails the build: any error (missing XML, no matching check
run, API error) degrades to a printed warning and exit 0.

Usage (from a workflow step, ``if: failure()``)::

    GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }} python scripts/annotate_test_failures.py \
        --xml test-results.xml --job build-windows
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

API = "https://api.github.com"
MAX_ANNOTATIONS_PER_REQUEST = 50
MAX_MESSAGE_CHARS = 2000


def _warn(message: str) -> None:
    print(f"[annotate-test-failures] WARNING: {message}", file=sys.stderr)


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _test_file_path(classname: str) -> str | None:
    """``tests.unit.test_x`` -> ``tests/unit/test_x.py``; None if unparseable."""
    parts = classname.split(".")
    if len(parts) < 3 or parts[0] != "tests" or parts[1] not in ("unit", "integration", "e2e"):
        return None
    return "/".join(parts) + ".py"


def collect_failures(xml_path: str) -> list[dict]:
    """Failing/erroring testcases from a junit XML file as annotations."""
    tree = ET.parse(xml_path)
    annotations: list[dict] = []
    for case in tree.getroot().iter("testcase"):
        problem = case.find("failure")
        level = "failure"
        if problem is None:
            problem = case.find("error")
            level = "failure"
        if problem is None:
            continue
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        path = _test_file_path(classname)
        if path is None:
            print(f"[annotate-test-failures] skipping unparseable classname: {classname!r}")
            continue
        detail = (problem.get("message") or "").strip()
        body = (problem.text or "").strip()
        text = f"{detail}\n{body}".strip()[:MAX_MESSAGE_CHARS]
        annotations.append(
            {
                "path": path,
                "start_line": 1,
                "end_line": 1,
                "annotation_level": level,
                "title": f"{classname}::{name}",
                "message": text or "(no failure detail in junit XML)",
            }
        )
    return annotations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", default="test-results.xml")
    parser.add_argument("--job", required=True, help="job/check-run name, e.g. build-windows")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    if not token or not repo or not sha:
        _warn("GITHUB_TOKEN / GITHUB_REPOSITORY / GITHUB_SHA not all set; skipping")
        return 0
    if not os.path.isfile(args.xml):
        _warn(f"no junit XML at {args.xml}; skipping")
        return 0

    try:
        annotations = collect_failures(args.xml)
    except Exception as exc:  # noqa: BLE001 — observability must never fail the build
        _warn(f"could not parse {args.xml}: {type(exc).__name__}: {exc}")
        return 0
    if not annotations:
        print("[annotate-test-failures] no failing testcases in junit XML")
        return 0

    try:
        runs = _api("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=30", token)
        check_run = next(
            (c for c in runs.get("check_runs", []) if c.get("name") == args.job),
            None,
        )
        if check_run is None:
            _warn(f"no check run named {args.job!r} for {sha[:7]}; skipping")
            return 0
        check_run_id = check_run["id"]
        for start in range(0, len(annotations), MAX_ANNOTATIONS_PER_REQUEST):
            _api(
                "POST",
                f"/repos/{repo}/check-runs/{check_run_id}/annotations",
                token,
                {"annotations": annotations[start : start + MAX_ANNOTATIONS_PER_REQUEST]},
            )
        print(f"[annotate-test-failures] posted {len(annotations)} annotation(s) to {args.job}")
    except urllib.error.HTTPError as exc:
        _warn(f"Checks API HTTP {exc.code}: {exc.read()[:300]!r}")
        return 0
    except Exception as exc:  # noqa: BLE001 — observability must never fail the build
        _warn(f"Checks API failed: {type(exc).__name__}: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

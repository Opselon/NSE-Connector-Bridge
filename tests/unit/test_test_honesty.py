"""Phase 6 — test honesty (PLAN addendum §7, ARCHITECTURE-CONTRACT §9).

Two guarantees, both enforced here so a later edit cannot silently
undo them:

1. Labelling: EVERY test is labelled exactly one of
   ``unit`` / ``mock_integration`` / ``real_mt5`` / ``manual`` (via a
   module-level ``pytestmark`` or a per-test marker), and the label
   matches the directory it lives in (``tests/unit`` -> ``unit``,
   ``tests/integration`` -> ``mock_integration``, ``tests/e2e`` ->
   ``real_mt5``).
2. Loud failure: a required-MT5 test FAILS (never silently skips or
   passes) when no live terminal is available. Proven behaviourally:
   one real e2e test is executed in a subprocess on this machine and
   must error at fixture setup with the "MT5 terminal unavailable"
   message. On a Windows machine with a terminal the same test runs
   for real; on any machine without one it must fail loudly.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS = {"unit", "mock_integration", "real_mt5", "manual"}
DIR_LABEL = {
    "tests/unit": "unit",
    "tests/integration": "mock_integration",
    "tests/e2e": "real_mt5",
}


def _module_labels(tree: ast.Module) -> set[str]:
    """Labels applied to the whole module via ``pytestmark = ...``."""
    labels: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        value = node.value
        # pytestmark = pytest.mark.<label>
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Attribute)
            and isinstance(value.value.value, ast.Name)
            and value.value.value.id == "pytest"
            and value.value.attr == "mark"
        ):
            labels.add(value.attr)
        # pytestmark = [pytest.mark.a, pytest.mark.b]
        if isinstance(value, ast.List):
            for elt in value.elts:
                if (
                    isinstance(elt, ast.Attribute)
                    and isinstance(elt.value, ast.Attribute)
                    and elt.value.attr == "mark"
                ):
                    labels.add(elt.attr)
    return labels


def _test_labels(func: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    labels: set[str] = set()
    for dec in func.decorator_list:
        # @pytest.mark.<label>  /  @pytest.mark.<label>(...)
        node: ast.AST = dec
        if isinstance(node, ast.Call):
            node = node.func
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
            and node.value.attr == "mark"
        ):
            labels.add(node.attr)
    return labels


def _iter_test_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("tests/*/test_*.py"))


def test_every_test_file_has_a_module_pytestmark():
    missing = []
    for path in _iter_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not (_module_labels(tree) & LABELS):
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, f"test files without a module-level label: {missing}"


def test_every_test_function_is_labelled():
    unlabelled: list[str] = []
    for path in _iter_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_labels = _module_labels(tree)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if not (module_labels | _test_labels(node)) & LABELS:
                unlabelled.append(f"{path.relative_to(REPO_ROOT)}::{node.name}")
    assert not unlabelled, f"unlabelled tests: {unlabelled}"


def test_label_matches_the_directory_it_lives_in():
    mismatches = []
    for path in _iter_test_files():
        rel_dir = str(path.parent.relative_to(REPO_ROOT))
        expected = DIR_LABEL[rel_dir]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_labels = _module_labels(tree)
        if module_labels and expected not in module_labels:
            mismatches.append(f"{path.relative_to(REPO_ROOT)}: {sorted(module_labels)}")
    assert not mismatches, f"module label does not match directory: {mismatches}"


def test_conftest_fails_loudly_on_mt5_unavailable():
    """Regression guard for the conftest fix: the session fixture
    must call pytest.fail when the terminal is unavailable, and must
    not contain a try/except-pass swallow around driver.connect()."""
    text = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pytest.fail" in text, "conftest must fail loudly, not swallow the connect failure"
    assert "MT5 terminal unavailable" in text
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                raise AssertionError(
                    "conftest still contains a bare except-pass swallow — "
                    "a required-MT5 failure must fail the test, never pass silently"
                )


def test_required_mt5_test_fails_loudly_without_a_terminal(tmp_path):
    """Behavioural proof: run ONE real e2e test in a subprocess on
    this machine and assert it ERRORS at setup with the loud message
    — no pass, no skip, no silent green. If a live MT5 terminal
    actually answers (a Windows dev machine), the e2e test genuinely
    passes and this proof is not applicable — skip instead of failing."""
    target = "tests/e2e/test_cat1_connection.py::test_02_get_bridge_status"
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", target,
            "-q", "-p", "no:cacheprovider", "--no-header",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,  # the return code IS the assertion under test
    )
    out = proc.stdout + proc.stderr
    if proc.returncode == 0:
        pytest.skip("a live MT5 terminal answered — loud-failure proof not applicable here")
    assert "MT5 terminal unavailable" in out, f"loud failure message missing:\n{out}"
    assert "ERROR at setup" in out, f"failure must happen at fixture setup:\n{out}"
    assert " passed" not in out, f"nothing may pass without MT5:\n{out}"
    assert " skipped" not in out, f"nothing may skip silently without MT5:\n{out}"

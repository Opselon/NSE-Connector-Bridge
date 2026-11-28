"""Unit tests for the P1-7 rollback / recovery strategy (docs/ROLLBACK.md).

Covers, with mocks only (no user PC, no MT5, no real download):
- failed verify at download: nothing staged, the running version is
  untouched and stays the fallback;
- failed verify at install: a staged file tampered after staging is
  refused, the target is untouched, no backup is created;
- previous-version retention: a good install backs the running exe
  up next to the target (+ rollback-state.json manifest) and the
  updater status reports it;
- failed start: a new version that exits inside the grace window is
  replaced by the restored previous version, which is relaunched;
- healthy start: a new version that stays up is kept, backup retained;
- fail-closed: a tampered or missing backup is never trusted
  (rollback_available False); a fresh install has no previous
  version to fall back to.

The fake "exes" are inert shell scripts (sleep / exit 1) — they are
never a real Bridge, and the staged bytes are never executed except
through the apply script's own restart launch, exactly as in
production. The two real-process start tests run on POSIX only:
Windows cannot execute a shell script as a .exe (WinError 193), so
on win32 they skip and the stubbed-launcher tests below carry the
start-check coverage on every platform, including the launch-failure
case (PR #37 Windows CI).
Label: unit.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys

import httpx
import pytest

from nse_bridge import BRIDGE_VERSION
from nse_bridge.config import BridgeSettings
from nse_bridge.server.rest import ReleaseInfo
from nse_bridge.updates import (
    APPLY_SCRIPT_TEMPLATE,
    Updater,
    read_rollback_state,
    rollback_backup_path,
)

pytestmark = pytest.mark.unit

#: A fake "healthy" exe: stays alive (the start check must pass it).
HEALTHY_EXE = b"#!/bin/sh\nsleep 60\n"
#: A fake "broken" exe: exits immediately (the start check must fail it).
BROKEN_EXE = b"#!/bin/sh\nexit 1\n"
OLD_BYTES = b"old-bytes"


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NSE_MT5_TERMINAL_PATH", raising=False)


def _make_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return priv, pub_b64


@pytest.fixture
def keypair():
    return _make_keypair()


def _release(priv, data, *, version="99.99.99", min_version="1.0.0", signature=True, sha=True):
    return ReleaseInfo(
        version=version,
        download_url="https://example.invalid/bridge-99.99.99.exe",
        sha256=hashlib.sha256(data).hexdigest() if sha else None,
        size_bytes=len(data),
        min_connector_version=min_version,
        signature=base64.b64encode(priv.sign(data)).decode() if signature else None,
        released_at="2026-11-18T00:00:00Z",
    )


@pytest.fixture
def settings(tmp_path, keypair):
    _, pub_b64 = keypair
    s = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    s.update_public_key = pub_b64
    s.server_token = "sk-test-secret-abcdef123456"
    return s


class FakeRestClient:
    def __init__(self, release=None):
        self.release = release

    async def releases_latest(self):
        return self.release


def _transport_for(data, *, status=200):
    def handler(request):
        assert request.url.path.endswith(".exe")
        return httpx.Response(status, content=data)

    return httpx.MockTransport(handler)


def _updater(settings, fake_rest, *, transport=None):
    return Updater(settings, rest_client_getter=lambda: fake_rest, download_transport=transport)


def _stage(settings, keypair, data, *, version="99.99.99"):
    """Download+verify+stage `data` through the real Updater; returns (updater, staged_path)."""
    priv, _ = keypair
    updater = _updater(
        settings, FakeRestClient(release=_release(priv, data, version=version)), transport=_transport_for(data)
    )
    assert asyncio.run(updater.download())["state"] == "staged"
    staged = settings.data_dir / "updates" / f"nse-bridge-{version}.exe"
    assert staged.is_file()
    return updater, staged


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _run_apply_script(
    script, *, staged, target, sha256, restart_cmd=None, previous_version=BRIDGE_VERSION, new_version="99.99.99"
):
    cmd = [
        sys.executable,
        str(script),
        "--staged",
        str(staged),
        "--target",
        str(target),
        "--parent-pid",
        str(_dead_pid()),
        "--sha256",
        sha256,
        "--previous-version",
        previous_version,
        "--new-version",
        new_version,
        "--start-grace-seconds",
        "2",
        "--rollback-observe-seconds",
        "2",
    ]
    if restart_cmd is not None:
        cmd += ["--restart-cmd", str(restart_cmd)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)


def _apply_result(settings):
    return json.loads((settings.data_dir / "updates" / "apply-result.json").read_text())


def _kill(pid):
    """Kill a launched fake exe and its children.

    The apply script launches the restart detached in a new session,
    so the recorded PID is a session leader — kill the whole process
    group, or the fake exe's `sleep` child survives as an orphan.
    """
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_fake_exe(path, data):
    path.write_bytes(data)
    os.chmod(path, 0o755)


# ---------------------------------------------------------------------------
# failed verify: download refuses, nothing staged, current version untouched
# ---------------------------------------------------------------------------
def test_failed_verify_download_leaves_current_version_untouched(settings, keypair):
    other_priv, _ = _make_keypair()  # release signed by a key we do not trust
    updater = _updater(
        settings,
        FakeRestClient(release=_release(other_priv, HEALTHY_EXE)),
        transport=_transport_for(HEALTHY_EXE),
    )
    status = asyncio.run(updater.download())
    assert status["state"] == "error"
    assert status["error"]["code"] == "SIGNATURE_INVALID"
    staging = settings.data_dir / "updates"
    assert not staging.is_dir() or [p for p in staging.iterdir() if p.suffix == ".exe"] == []
    assert status["installed_version"] == BRIDGE_VERSION
    assert status["staged_version"] is None
    assert status["rollback_available"] is False
    assert status["previous_version"] is None


def test_update_state_records_installed_version(settings, keypair):
    updater, _ = _stage(settings, keypair, HEALTHY_EXE)
    state = json.loads((settings.data_dir / "updates" / "update-state.json").read_text())
    assert state["installed_version"] == BRIDGE_VERSION
    assert state["version"] == "99.99.99"
    assert updater.status()["staged_version"] == "99.99.99"


# ---------------------------------------------------------------------------
# retention: good install backs the previous version up + manifests it
# ---------------------------------------------------------------------------
def test_apply_script_backs_up_previous_version(settings, keypair, tmp_path):
    updater, staged = _stage(settings, keypair, HEALTHY_EXE)
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(OLD_BYTES)
    script = updater.write_apply_script()
    assert APPLY_SCRIPT_TEMPLATE in script.read_text()
    result = _run_apply_script(script, staged=staged, target=target, sha256=hashlib.sha256(HEALTHY_EXE).hexdigest())
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == HEALTHY_EXE
    backup = rollback_backup_path(target)
    assert backup.read_bytes() == OLD_BYTES
    state = json.loads((settings.data_dir / "updates" / "rollback-state.json").read_text())
    assert state["previous_version"] == BRIDGE_VERSION
    assert state["previous_sha256"] == hashlib.sha256(OLD_BYTES).hexdigest()
    assert state["previous_path"] == str(backup)
    assert state["new_version"] == "99.99.99"
    assert state["new_sha256"] == hashlib.sha256(HEALTHY_EXE).hexdigest()
    assert state["available"] is True
    apply_result = _apply_result(settings)
    assert apply_result["ok"] is True
    assert apply_result["rolled_back"] is False
    assert apply_result["rollback_available"] is True
    assert apply_result["start_check"] == "skipped_no_restart"
    # The retained version is verifiable through the host-side reader
    # and surfaced in the updater status.
    assert read_rollback_state(settings.data_dir / "updates") is not None
    status = updater.status()
    assert status["rollback_available"] is True
    assert status["previous_version"] == BRIDGE_VERSION
    assert status["previous_sha256"] == hashlib.sha256(OLD_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# failed verify at install: tampered staged file touches nothing
# ---------------------------------------------------------------------------
def test_apply_script_tampered_staged_file_touches_nothing(settings, keypair, tmp_path):
    updater, staged = _stage(settings, keypair, HEALTHY_EXE)
    staged.write_bytes(b"tampered-after-staging")
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(OLD_BYTES)
    script = updater.write_apply_script()
    result = _run_apply_script(script, staged=staged, target=target, sha256=hashlib.sha256(HEALTHY_EXE).hexdigest())
    assert result.returncode != 0
    assert target.read_bytes() == OLD_BYTES  # untouched
    assert not rollback_backup_path(target).exists()  # no backup was taken
    assert not (settings.data_dir / "updates" / "rollback-state.json").exists()
    apply_result = _apply_result(settings)
    assert apply_result["ok"] is False
    assert "SHA-256 mismatch" in (apply_result["error"] or "")
    assert apply_result["rolled_back"] is False


# ---------------------------------------------------------------------------
# failed start: new version exits in the grace window -> previous restored
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute a shell-script .exe (WinError 193); the launch path is covered by the stubbed-launcher tests",
)
def test_failed_start_falls_back_to_previous_version(settings, keypair, tmp_path):
    updater, staged = _stage(settings, keypair, BROKEN_EXE)
    target = tmp_path / "dummy-target.exe"
    _write_fake_exe(target, HEALTHY_EXE)  # the previous version stays alive
    script = updater.write_apply_script()
    result = _run_apply_script(
        script, staged=staged, target=target, sha256=hashlib.sha256(BROKEN_EXE).hexdigest(), restart_cmd=target
    )
    apply_result = _apply_result(settings)
    try:
        assert result.returncode != 0, result.stderr  # the update failed
        assert apply_result["ok"] is False
        assert apply_result["rolled_back"] is True
        assert apply_result["rollback_restored"] is True
        assert apply_result["rollback_launch_failed"] is False
        assert target.read_bytes() == HEALTHY_EXE  # previous version restored
        assert rollback_backup_path(target).read_bytes() == HEALTHY_EXE
        assert apply_result["rollback_pid"] is not None
        assert _pid_alive(apply_result["rollback_pid"])  # relaunched previous is running
    finally:
        _kill(apply_result.get("rollback_pid"))
        _kill(apply_result.get("restarted_pid"))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot execute a shell-script .exe (WinError 193); the launch path is covered by the stubbed-launcher tests",
)
def test_healthy_start_keeps_new_version(settings, keypair, tmp_path):
    updater, staged = _stage(settings, keypair, HEALTHY_EXE)
    target = tmp_path / "dummy-target.exe"
    previous_exe = b"#!/bin/sh\nsleep 60\n# previous version\n"  # distinct bytes, also stays alive
    _write_fake_exe(target, previous_exe)
    script = updater.write_apply_script()
    result = _run_apply_script(
        script, staged=staged, target=target, sha256=hashlib.sha256(HEALTHY_EXE).hexdigest(), restart_cmd=target
    )
    apply_result = _apply_result(settings)
    try:
        assert result.returncode == 0, result.stderr
        assert apply_result["ok"] is True
        assert apply_result["rolled_back"] is False
        assert apply_result["start_check"] == "passed"
        assert target.read_bytes() == HEALTHY_EXE
        assert rollback_backup_path(target).read_bytes() == previous_exe
        assert apply_result["restarted_pid"] is not None
        assert _pid_alive(apply_result["restarted_pid"])  # new version still running
    finally:
        _kill(apply_result.get("restarted_pid"))


# ---------------------------------------------------------------------------
# fail-closed: unverifiable or absent previous version is never trusted
# ---------------------------------------------------------------------------
def test_rollback_state_unverifiable_when_backup_tampered(settings, tmp_path):
    staging = settings.data_dir / "updates"
    staging.mkdir(parents=True)
    backup = tmp_path / "prev.exe.previous"
    backup.write_bytes(OLD_BYTES)
    (staging / "rollback-state.json").write_text(
        json.dumps(
            {
                "previous_version": "1.4.0",
                "previous_sha256": hashlib.sha256(OLD_BYTES).hexdigest(),
                "previous_path": str(backup),
                "target": str(tmp_path / "prev.exe"),
                "new_version": "1.4.1",
                "new_sha256": hashlib.sha256(HEALTHY_EXE).hexdigest(),
                "available": True,
            }
        )
    )
    assert read_rollback_state(staging) is not None  # intact backup verifies
    backup.write_bytes(b"tampered")  # corrupt the retained exe
    assert read_rollback_state(staging) is None
    updater = Updater(settings, rest_client_getter=lambda: None)
    status = updater.status()
    assert status["rollback_available"] is False
    assert status["previous_version"] is None


def test_rollback_state_missing_backup_is_not_available(settings):
    staging = settings.data_dir / "updates"
    staging.mkdir(parents=True)
    (staging / "rollback-state.json").write_text(
        json.dumps(
            {
                "previous_version": "1.4.0",
                "previous_sha256": hashlib.sha256(OLD_BYTES).hexdigest(),
                "previous_path": str(staging / "gone.exe.previous"),
                "target": str(staging / "gone.exe"),
                "new_version": "1.4.1",
                "new_sha256": hashlib.sha256(HEALTHY_EXE).hexdigest(),
                "available": True,
            }
        )
    )
    assert read_rollback_state(staging) is None


def test_fresh_install_has_no_previous_version(settings, keypair, tmp_path):
    """No target yet: the install proceeds, but there is nothing to
    fall back to — recorded explicitly, never invented."""
    updater, staged = _stage(settings, keypair, HEALTHY_EXE)
    target = tmp_path / "dummy-target.exe"  # does not exist
    script = updater.write_apply_script()
    result = _run_apply_script(script, staged=staged, target=target, sha256=hashlib.sha256(HEALTHY_EXE).hexdigest())
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == HEALTHY_EXE
    assert not rollback_backup_path(target).exists()
    state = json.loads((settings.data_dir / "updates" / "rollback-state.json").read_text())
    assert state["previous_version"] is None
    assert state["available"] is False
    apply_result = _apply_result(settings)
    assert apply_result["ok"] is True
    assert apply_result["rollback_available"] is False
    assert read_rollback_state(settings.data_dir / "updates") is None


def test_apply_script_template_stays_ascii():
    assert APPLY_SCRIPT_TEMPLATE.isascii()


# ---------------------------------------------------------------------------
# start check via a stubbed launcher (runs on every platform, incl.
# Windows — the shell-script fake exes above cannot execute there,
# so the subprocess tests skip on win32 and these carry the coverage)
# ---------------------------------------------------------------------------
def _template_ns() -> dict:
    ns: dict = {"__name__": "apply_update_test"}
    # The test's subject IS the exec'd template; the string is our own
    # module constant, never user input.
    exec(APPLY_SCRIPT_TEMPLATE, ns)  # noqa: S102
    return ns


class _FakeProc:
    """Stand-in for subprocess.Popen: poll() None = still running."""

    def __init__(self, *, alive, pid=4242):
        self._alive = alive
        self.pid = pid

    def poll(self):
        return None if self._alive else 1


def _stub_launcher(behaviours):
    """A _launch_detached stub: behaviour per call (a _FakeProc, or an
    exception to raise); the last behaviour repeats for later calls."""
    calls = {"n": 0}

    def launch(argv):
        idx = calls["n"]
        calls["n"] += 1
        behaviour = behaviours[min(idx, len(behaviours) - 1)]
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    launch.calls = calls
    return launch


def _run_apply_main(ns, monkeypatch, *, staged, target, sha256):
    """Run the template's main() in-process with a stubbed launcher
    already installed in the namespace; returns its exit code."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_update.py",
            "--staged",
            str(staged),
            "--target",
            str(target),
            "--parent-pid",
            str(_dead_pid()),
            "--sha256",
            sha256,
            "--previous-version",
            BRIDGE_VERSION,
            "--new-version",
            "99.99.99",
            "--start-grace-seconds",
            "1",
            "--rollback-observe-seconds",
            "1",
            "--restart-cmd",
            str(target),
        ],
    )
    return ns["main"]()


def test_failed_start_falls_back_via_stubbed_launcher(settings, keypair, tmp_path, monkeypatch):
    _, staged = _stage(settings, keypair, BROKEN_EXE)
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(HEALTHY_EXE)  # the previous version
    ns = _template_ns()
    ns["_launch_detached"] = _stub_launcher([_FakeProc(alive=False, pid=4242), _FakeProc(alive=True, pid=4243)])
    exit_code = _run_apply_main(
        ns, monkeypatch, staged=staged, target=target, sha256=hashlib.sha256(BROKEN_EXE).hexdigest()
    )
    apply_result = _apply_result(settings)
    assert exit_code == 1
    assert apply_result["ok"] is False
    assert apply_result["rolled_back"] is True
    assert apply_result["rollback_restored"] is True
    assert apply_result["rollback_launch_failed"] is False
    assert apply_result["rollback_process_alive"] is True
    assert apply_result["rollback_pid"] == 4243
    assert target.read_bytes() == HEALTHY_EXE  # previous version restored


def test_healthy_start_keeps_new_version_via_stubbed_launcher(settings, keypair, tmp_path, monkeypatch):
    _, staged = _stage(settings, keypair, HEALTHY_EXE)
    target = tmp_path / "dummy-target.exe"
    previous_exe = b"#!/bin/sh\nsleep 60\n# previous version\n"
    target.write_bytes(previous_exe)
    ns = _template_ns()
    ns["_launch_detached"] = _stub_launcher([_FakeProc(alive=True, pid=4242)])
    exit_code = _run_apply_main(
        ns, monkeypatch, staged=staged, target=target, sha256=hashlib.sha256(HEALTHY_EXE).hexdigest()
    )
    apply_result = _apply_result(settings)
    assert exit_code == 0
    assert apply_result["ok"] is True
    assert apply_result["rolled_back"] is False
    assert apply_result["start_check"] == "passed"
    assert apply_result["restarted_pid"] == 4242
    assert target.read_bytes() == HEALTHY_EXE
    assert rollback_backup_path(target).read_bytes() == previous_exe


def test_launch_failure_falls_back_to_previous_version(settings, keypair, tmp_path, monkeypatch):
    """The Windows PR #37 scenario: the new exe cannot be launched
    at all (WinError 193) — that is a failed start, not a successful
    install, and the previous version is restored and relaunched."""
    _, staged = _stage(settings, keypair, BROKEN_EXE)
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(HEALTHY_EXE)  # the previous version
    ns = _template_ns()
    ns["_launch_detached"] = _stub_launcher(
        [
            OSError("[WinError 193] %1 is not a valid Win32 application"),
            _FakeProc(alive=True, pid=4243),
        ]
    )
    exit_code = _run_apply_main(
        ns, monkeypatch, staged=staged, target=target, sha256=hashlib.sha256(BROKEN_EXE).hexdigest()
    )
    apply_result = _apply_result(settings)
    assert exit_code == 1
    assert apply_result["ok"] is False
    assert "failed to launch" in (apply_result["error"] or "")
    assert apply_result["rolled_back"] is True
    assert apply_result["rollback_restored"] is True
    assert apply_result["rollback_launch_failed"] is False
    assert apply_result["rollback_process_alive"] is True
    assert target.read_bytes() == HEALTHY_EXE  # previous version restored


def test_launch_failure_on_both_attempts_reports_rollback_launch_failed(settings, keypair, tmp_path, monkeypatch):
    """Fail-closed: when even the restored previous version cannot be
    launched, the failure is recorded (never reported as success)."""
    _, staged = _stage(settings, keypair, BROKEN_EXE)
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(HEALTHY_EXE)  # the previous version
    ns = _template_ns()
    ns["_launch_detached"] = _stub_launcher(
        [
            OSError("[WinError 193] %1 is not a valid Win32 application"),
            OSError("[WinError 193] %1 is not a valid Win32 application"),
        ]
    )
    exit_code = _run_apply_main(
        ns, monkeypatch, staged=staged, target=target, sha256=hashlib.sha256(BROKEN_EXE).hexdigest()
    )
    apply_result = _apply_result(settings)
    assert exit_code == 1
    assert apply_result["ok"] is False
    assert apply_result["rolled_back"] is True
    assert apply_result["rollback_restored"] is True
    assert apply_result["rollback_launch_failed"] is True
    assert apply_result["rollback_pid"] is None
    assert target.read_bytes() == HEALTHY_EXE  # restored on disk even so

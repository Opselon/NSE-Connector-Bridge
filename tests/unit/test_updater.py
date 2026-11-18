"""Unit tests for the Phase 5 verified updater.

Covers: SHA-256 match/mismatch, Ed25519 valid signature passes /
tampered bytes fail / null signature refuses / no public key refuses /
min_connector_version respected, staging (verified file staged,
unverified file never staged), the restart-installer script mechanics
with inert dummy files (the staged bytes are never executed), and the
local endpoints (check / download / status) incl. mandatory auth.
No test performs a real download — all downloads go through an
httpx.MockTransport serving fixture bytes.
Label: unit.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from nse_bridge import BRIDGE_VERSION
from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.errors import ServerError, ServerErrorCode
from nse_bridge.server.rest import ReleaseInfo
from nse_bridge.updates import (
    APPLY_SCRIPT_TEMPLATE,
    UpdateError,
    Updater,
    _resolve_python_executable,
    load_update_public_key,
    verify_artifact,
)

pytestmark = pytest.mark.unit

ARTIFACT = b"NSE-BRIDGE-FAKE-ARTIFACT-v1.2.0" + b"\x00" * 64
OTHER_BYTES = bytes([ARTIFACT[0] ^ 0xFF]) + ARTIFACT[1:]  # same length, different bytes


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


def _public_key(pub_b64):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))


def _release(priv, data, *, version="1.2.0", min_version="1.0.0", signature=True, sha=True, size=None):
    return ReleaseInfo(
        version=version,
        download_url="https://example.invalid/bridge-1.2.0.exe",
        sha256=hashlib.sha256(data).hexdigest() if sha else None,
        size_bytes=len(data) if size is None else size,
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
    """Minimal stand-in for the Phase 2 REST client (releases only)."""

    def __init__(self, release=None, release_error=None):
        self.release = release
        self.release_error = release_error

    async def releases_latest(self):
        if self.release_error is not None:
            raise self.release_error
        return self.release


def _transport_for(data, *, status=200):
    def handler(request):
        assert request.url.path.endswith(".exe")
        return httpx.Response(status, content=data)

    return httpx.MockTransport(handler)


def _updater(settings, fake_rest, *, transport=None):
    return Updater(
        settings,
        rest_client_getter=lambda: fake_rest,
        download_transport=transport,
    )


def _staged_files(settings):
    staging = settings.data_dir / "updates"
    if not staging.is_dir():
        return []
    return [p for p in staging.iterdir() if p.suffix == ".exe"]


# ---------------------------------------------------------------------------
# verification primitives
# ---------------------------------------------------------------------------
def test_verify_artifact_valid_signature_passes(keypair, settings):
    priv, pub_b64 = keypair
    verify_artifact(ARTIFACT, _release(priv, ARTIFACT), _public_key(pub_b64))


def test_verify_artifact_tampered_bytes_fail_sha(keypair):
    priv, pub_b64 = keypair
    tampered = ARTIFACT[:10] + b"X" + ARTIFACT[11:]  # same length, one byte flipped
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(tampered, _release(priv, ARTIFACT), _public_key(pub_b64))
    assert excinfo.value.code == "SHA256_MISMATCH"


def test_verify_artifact_wrong_signature_fails(keypair):
    priv, pub_b64 = keypair
    # Correct SHA-256 for ARTIFACT, but a signature over OTHER_BYTES.
    release = _release(priv, OTHER_BYTES)
    release.sha256 = hashlib.sha256(ARTIFACT).hexdigest()
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(ARTIFACT, release, _public_key(pub_b64))
    assert excinfo.value.code == "SIGNATURE_INVALID"


def test_verify_artifact_null_signature_refuses(keypair):
    priv, pub_b64 = keypair
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(ARTIFACT, _release(priv, ARTIFACT, signature=False), _public_key(pub_b64))
    assert excinfo.value.code == "SIGNATURE_MISSING"


def test_verify_artifact_missing_sha_refuses(keypair):
    priv, pub_b64 = keypair
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(ARTIFACT, _release(priv, ARTIFACT, sha=False), _public_key(pub_b64))
    assert excinfo.value.code == "SHA256_MISSING"


def test_verify_artifact_size_mismatch_refuses(keypair):
    priv, pub_b64 = keypair
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(ARTIFACT, _release(priv, ARTIFACT, size=len(ARTIFACT) + 1), _public_key(pub_b64))
    assert excinfo.value.code == "SIZE_MISMATCH"


def test_load_public_key_none_when_unconfigured(tmp_path):
    s = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert load_update_public_key(s) is None


def test_load_public_key_malformed_refuses(tmp_path):
    s = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    s.update_public_key = "!!!not-base64!!!"
    with pytest.raises(UpdateError) as excinfo:
        load_update_public_key(s)
    assert excinfo.value.code == "INVALID_PUBLIC_KEY"
    s.update_public_key = base64.b64encode(b"too-short").decode()
    with pytest.raises(UpdateError) as excinfo:
        load_update_public_key(s)
    assert excinfo.value.code == "INVALID_PUBLIC_KEY"


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------
async def test_check_reports_update_available(settings, keypair):
    priv, _ = keypair
    updater = _updater(settings, FakeRestClient(release=_release(priv, ARTIFACT)))
    status = await updater.check()
    assert status["state"] == "update_available"
    assert status["update_available"] is True
    assert status["latest"]["version"] == "1.2.0"
    assert status["latest"]["signature_present"] is True
    assert status["error"] is None


async def test_check_up_to_date(settings, keypair):
    priv, _ = keypair
    updater = _updater(settings, FakeRestClient(release=_release(priv, ARTIFACT, version=BRIDGE_VERSION)))
    status = await updater.check()
    assert status["state"] == "up_to_date"
    assert status["update_available"] is False


async def test_check_incompatible_min_version(settings, keypair):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT, version="1.2.0", min_version="9.9.9")),
    )
    status = await updater.check()
    assert status["state"] == "incompatible"
    assert status["error"]["code"] == "MIN_VERSION_NOT_SATISFIED"
    assert status["update_available"] is False


async def test_check_not_configured_without_token(settings, keypair):
    priv, _ = keypair
    settings.server_token = ""
    updater = _updater(settings, FakeRestClient(release=_release(priv, ARTIFACT)))
    status = await updater.check()
    assert status["state"] == "not_configured"
    assert status["error"]["code"] == "NOT_CONFIGURED"


async def test_check_failed_on_server_error(settings):
    updater = _updater(
        settings,
        FakeRestClient(release_error=ServerError(ServerErrorCode.SERVER_UNAVAILABLE, "server down", http_status=503)),
    )
    status = await updater.check()
    assert status["state"] == "error"
    assert status["error"] is not None


# ---------------------------------------------------------------------------
# download() — staging only verified bytes
# ---------------------------------------------------------------------------
async def test_download_stages_verified_file(settings, keypair):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    status = await updater.download()
    assert status["state"] == "staged", status
    assert status["restart_required"] is True
    assert status["staged_version"] == "1.2.0"
    assert status["verified"] is True
    staged = _staged_files(settings)
    assert len(staged) == 1
    assert staged[0].read_bytes() == ARTIFACT  # exact bytes, staged not executed
    state_file = settings.data_dir / "updates" / "update-state.json"
    state = json.loads(state_file.read_text())
    assert state["signature_verified"] is True
    assert state["sha256"] == hashlib.sha256(ARTIFACT).hexdigest()
    assert (settings.data_dir / "updates" / "apply_update.py").is_file()


async def test_download_refuses_without_public_key(settings, keypair):
    priv, _ = keypair
    settings.update_public_key = ""
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    status = await updater.download()
    assert status["state"] == "error", status
    assert status["error"]["code"] == "NO_PUBLIC_KEY"
    assert "no update public key" in status["error"]["message"]
    assert _staged_files(settings) == []


async def test_download_refuses_null_signature(settings, keypair):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT, signature=False)),
        transport=_transport_for(ARTIFACT),
    )
    status = await updater.download()
    assert status["error"]["code"] == "SIGNATURE_MISSING"
    assert _staged_files(settings) == []


async def test_download_refuses_bad_sha(settings, keypair):
    priv, _ = keypair
    bad = _release(priv, ARTIFACT)
    bad.sha256 = "00" * 32
    updater = _updater(settings, FakeRestClient(release=bad), transport=_transport_for(ARTIFACT))
    status = await updater.download()
    assert status["error"]["code"] == "SHA256_MISMATCH"
    assert _staged_files(settings) == []


async def test_download_never_stages_unverified_bytes(settings, keypair):
    priv, _ = keypair
    # Server metadata signs ARTIFACT, but the transport serves OTHER_BYTES.
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(OTHER_BYTES),
    )
    status = await updater.download()
    assert status["error"]["code"] in ("SHA256_MISMATCH", "SIZE_MISMATCH", "SIGNATURE_INVALID")
    assert _staged_files(settings) == []


async def test_download_refuses_min_version_without_downloading(settings, keypair):
    priv, _ = keypair

    def handler(request):
        raise AssertionError("must not download an incompatible release")

    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT, version="1.2.0", min_version="9.9.9")),
        transport=httpx.MockTransport(handler),
    )
    status = await updater.download()
    assert status["state"] == "incompatible"
    assert status["error"]["code"] == "MIN_VERSION_NOT_SATISFIED"
    assert _staged_files(settings) == []


async def test_download_refuses_when_up_to_date(settings, keypair):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT, version=BRIDGE_VERSION)),
        transport=_transport_for(ARTIFACT),
    )
    status = await updater.download()
    assert status["error"]["code"] == "NO_UPDATE_AVAILABLE"
    assert _staged_files(settings) == []


# ---------------------------------------------------------------------------
# restart installer script (dummy files only — staged bytes never executed)
# ---------------------------------------------------------------------------
def test_apply_script_replaces_dummy_target(settings, keypair, tmp_path):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    status = asyncio.run(updater.download())
    assert status["state"] == "staged"
    staged = _staged_files(settings)[0]
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(b"old-bytes")
    # A PID that has already exited (the script waits for it to disappear).
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    script = updater.write_apply_script()
    assert APPLY_SCRIPT_TEMPLATE in script.read_text()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--staged",
            str(staged),
            "--target",
            str(target),
            "--parent-pid",
            str(proc.pid),
            "--sha256",
            hashlib.sha256(ARTIFACT).hexdigest(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == ARTIFACT
    apply_result = json.loads((settings.data_dir / "updates" / "apply-result.json").read_text())
    assert apply_result["ok"] is True


def test_apply_script_refuses_wrong_sha(settings, keypair, tmp_path):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    assert asyncio.run(updater.download())["state"] == "staged"
    staged = _staged_files(settings)[0]
    target = tmp_path / "dummy-target.exe"
    target.write_bytes(b"old-bytes")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    script = updater.write_apply_script()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--staged",
            str(staged),
            "--target",
            str(target),
            "--parent-pid",
            str(proc.pid),
            "--sha256",
            "00" * 32,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert target.read_bytes() == b"old-bytes"  # untouched


def test_apply_script_template_is_ascii_and_survives_cp1252_read(settings, keypair):
    """Regression: run 44272982805 — the staged script contained em dashes
    (U+2014). It is written as UTF-8, but on Windows ``read_text()`` with
    no encoding defaults to the locale encoding (cp1252), which decodes
    the UTF-8 em dash as different characters — so the template no longer
    matched the staged file and the test failed Windows-only. The template
    must stay pure ASCII so it round-trips under any single-byte read.
    """
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    assert APPLY_SCRIPT_TEMPLATE.isascii()
    script = updater.write_apply_script()
    assert APPLY_SCRIPT_TEMPLATE in script.read_text(encoding="cp1252")
    assert APPLY_SCRIPT_TEMPLATE in script.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# apply-script internals (the template exec'd; the Windows branch is
# testable on Linux by stubbing ctypes.WinDLL)
# ---------------------------------------------------------------------------
def _template_ns() -> dict:
    ns: dict = {"__name__": "apply_update_test"}
    # The test's subject IS the exec'd template; the string is our own
    # module constant, never user input.
    exec(APPLY_SCRIPT_TEMPLATE, ns)  # noqa: S102
    return ns


@pytest.mark.unit
def test_apply_script_pid_gone_posix_dead_pid():
    ns = _template_ns()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert ns["_pid_gone"](proc.pid) is True
    assert ns["_pid_gone"](os.getpid()) is False


@pytest.mark.unit
def test_apply_script_pid_gone_windows_uses_exit_code(monkeypatch):
    """os.kill(pid, 0) on Windows only proves the PID is openable — a
    dead process whose handle is still open (the launcher's Popen
    object) still opens, so the script waited the full 120s and the
    CI test timed out at 60s. The Windows branch checks the exit
    code instead: STILL_ACTIVE (259) means running."""
    import ctypes

    ns = _template_ns()
    monkeypatch.setattr(sys, "platform", "win32", raising=False)

    class _FakeKernel32:
        def __init__(self, *, handle=1234, exit_code=0, query_ok=True):
            self.handle = handle
            self.exit_code = exit_code
            self.query_ok = query_ok
            self.closed: list[int] = []

        def OpenProcess(self, access, inherit, pid):
            return self.handle

        def GetExitCodeProcess(self, handle, lpcode):
            if not self.query_ok:
                return 0
            ctypes.cast(lpcode, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.exit_code
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1

    fake = _FakeKernel32(exit_code=259)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    assert ns["_pid_gone"](4242) is False  # STILL_ACTIVE → alive
    assert fake.closed == [1234]  # handle released

    fake = _FakeKernel32(exit_code=0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    assert ns["_pid_gone"](4242) is True  # exited → gone

    fake = _FakeKernel32(handle=0)  # cannot open → gone
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    assert ns["_pid_gone"](4242) is True

    fake = _FakeKernel32(exit_code=259, query_ok=False)  # query fails → gone (safe: replace refuses)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    assert ns["_pid_gone"](4242) is True


@pytest.mark.unit
def test_apply_script_replace_retries_transient_lock(monkeypatch, tmp_path):
    """A freshly written .exe is often briefly locked by antivirus
    scanning; the replace must retry instead of losing a verified
    update."""
    ns = _template_ns()
    src = tmp_path / "staged.exe"
    dst = tmp_path / "target.exe"
    src.write_bytes(b"new-bytes")
    dst.write_bytes(b"old-bytes")
    attempts = {"n": 0}
    real_replace = os.replace

    def flaky_replace(s, d):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError("locked by scanner")
        return real_replace(s, d)

    monkeypatch.setattr(os, "replace", flaky_replace)
    ns["_replace_file"](str(src), str(dst))
    assert attempts["n"] == 3
    assert dst.read_bytes() == b"new-bytes"
    assert not src.exists()


@pytest.mark.unit
def test_apply_script_replace_falls_back_to_copy(monkeypatch, tmp_path):
    """A non-lock OSError (e.g. cross-device) falls through to copy+remove."""
    ns = _template_ns()
    src = tmp_path / "staged.exe"
    dst = tmp_path / "target.exe"
    src.write_bytes(b"new-bytes")
    dst.write_bytes(b"old-bytes")
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("Invalid cross-device link")))
    ns["_replace_file"](str(src), str(dst))
    assert dst.read_bytes() == b"new-bytes"
    assert not src.exists()


# ---------------------------------------------------------------------------
# frozen-build interpreter resolution (Codex P2)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_resolve_python_executable_not_frozen():
    assert _resolve_python_executable() == sys.executable


@pytest.mark.unit
def test_resolve_python_executable_frozen_prefers_system_python(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/python3" if name == "python" else None)
    assert _resolve_python_executable() == "/usr/bin/python3"


@pytest.mark.unit
def test_resolve_python_executable_frozen_none_without_python(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _resolve_python_executable() is None


async def test_launch_restart_installer_frozen_without_python_reports_honestly(settings, keypair, monkeypatch):
    """In the frozen build sys.executable is the bridge .exe — launching
    it with a .py script argument re-launches the bridge instead of
    running the script. With no system Python the launch must refuse
    honestly, not launch garbage."""
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    assert (await updater.download())["state"] == "staged"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    outcome = updater.launch_restart_installer()
    assert outcome["launched"] is False
    assert outcome["reason"] == "no_python_interpreter"


async def test_launch_restart_installer_reports_not_frozen_on_linux(settings, keypair):
    priv, _ = keypair
    updater = _updater(
        settings,
        FakeRestClient(release=_release(priv, ARTIFACT)),
        transport=_transport_for(ARTIFACT),
    )
    assert (await updater.download())["state"] == "staged"
    assert updater.install_target is None  # not a frozen exe run
    outcome = updater.launch_restart_installer()
    assert outcome["launched"] is False
    assert outcome["reason"] == "not_frozen_no_target"


def test_status_reports_persisted_staged_update(settings, keypair):
    # A staged update from a previous process shows up via update-state.json.
    staging = settings.data_dir / "updates"
    staging.mkdir(parents=True)
    staged = staging / "nse-bridge-1.2.0.exe"
    staged.write_bytes(ARTIFACT)
    (staging / "update-state.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "staged_path": str(staged),
                "sha256": hashlib.sha256(ARTIFACT).hexdigest(),
                "size_bytes": len(ARTIFACT),
                "signature_verified": True,
            }
        )
    )
    updater = Updater(settings, rest_client_getter=lambda: None)
    status = updater.status()
    assert status["state"] == "staged"
    assert status["staged_version"] == "1.2.0"
    assert status["restart_required"] is True


# ---------------------------------------------------------------------------
# local endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, keypair):
    _, pub_b64 = keypair
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    settings.update_public_key = pub_b64
    settings.server_token = "sk-test-secret-abcdef123456"
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx, app, settings
    driver.disconnect()


def _auth(ctx):
    return {"X-API-Key": ctx.settings.local_api_key}


def _wire_updater(client, keypair, *, transport):
    http, ctx, app, settings = client
    priv, _ = keypair
    ctx.rest_client = FakeRestClient(release=_release(priv, ARTIFACT))
    app.state.updater = Updater(settings, rest_client_getter=lambda: ctx.rest_client, download_transport=transport)
    return http, ctx, settings


def test_updates_endpoints_require_auth(client):
    http, _, _, _ = client
    for method, path in [
        ("GET", "/api/v1/updates/status"),
        ("POST", "/api/v1/updates/check"),
        ("POST", "/api/v1/updates/download"),
    ]:
        resp = http.request(method, path)
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"


def test_updates_check_download_status_endpoints(client, keypair):
    http, ctx, settings = _wire_updater(client, keypair, transport=_transport_for(ARTIFACT))
    resp = http.post("/api/v1/updates/check", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "update_available"
    assert data["update_available"] is True
    resp = http.post("/api/v1/updates/download", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "staged", data
    assert data["restart_required"] is True
    resp = http.get("/api/v1/updates/status", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["staged_version"] == "1.2.0"
    assert data["update_public_key_configured"] is True
    assert _staged_files(settings) != []


def test_updates_download_endpoint_refuses_without_public_key(client, keypair):
    http, ctx, settings = _wire_updater(client, keypair, transport=_transport_for(ARTIFACT))
    settings.update_public_key = ""
    resp = http.post("/api/v1/updates/download", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "error"
    assert data["error"]["code"] == "NO_PUBLIC_KEY"
    assert _staged_files(settings) == []

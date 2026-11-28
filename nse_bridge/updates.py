"""Verified updater (Phase 5, CONTRACT v1 §3.5).

Polls ``GET /api/v1/connector/releases/latest`` through the Phase 2
REST client, downloads the artifact, and verifies SHA-256 **and** the
Ed25519 signature **before** any install. Rules, fail-closed:

- No public key configured → refuse to install, structured error
  ``NO_PUBLIC_KEY`` ("signature unverifiable — no key configured").
  The key comes from Bridge configuration (env
  ``NSE_BRIDGE_UPDATE_PUBLIC_KEY``, base64 raw 32-byte Ed25519 public
  key) or the bundled ``nse_bridge/update_public_key.b64`` file —
  provisioned by the P1-6 signing pipeline (the NSE release-signing
  public key; the matching private key is held only in the GitHub
  Actions secret ``NSE_BRIDGE_RELEASE_SIGNING_KEY`` and the operator's
  FTP backup, never in this repo). A signed release is therefore
  installable; an unsigned release still refuses fail-closed.
- ``signature`` null in the release metadata → the release is
  unverifiable → refuse (``SIGNATURE_MISSING``).
- SHA-256 mismatch, size mismatch (when ``size_bytes`` is present),
  or a bad signature → refuse; the bytes are never staged.
- ``min_connector_version`` newer than the running version → refuse
  (``MIN_VERSION_NOT_SATISFIED``).
- Download-only staging: verified bytes are staged under
  ``<data_dir>/updates/``; nothing is executed or installed here.
  Install happens on restart via a small staged, self-contained
  Python script (``apply_update.py`` — our own code, not the
  downloaded artifact) that waits for the bridge process to exit,
  re-verifies the staged file's SHA-256, then replaces the target
  exe. The exe swap itself can only be proven on Windows (the
  packaged exe target only exists when frozen); on Linux/dev runs
  there is no exe target and the installer honestly reports
  ``not_frozen_no_target`` instead of pretending to install.

No downloaded byte is ever executed — including in tests, which use
inert fixtures and never perform a real download.

Rollback / recovery (P1-7, docs/ROLLBACK.md): before the target exe
is replaced, the apply script copies the running exe aside next to
it (``<target>.previous``) and records its version + SHA-256 in
``rollback-state.json`` in the staging dir — exactly one previous
generation is retained. The staged file's SHA-256 is re-verified
before any replacement (a failed re-check leaves the target
untouched: the running version IS the fallback). After the restart
the script watches the new process for a grace window (default
15 s); if it cannot be launched at all, or exits in that window,
the backup is restored over the target and the previous version is
relaunched, recorded as ``rolled_back`` in ``apply-result.json``.
Fail-closed throughout: a missing or SHA-mismatched backup is never
launched, and a fresh install (no previous version) has nothing to
fall back to — both cases are recorded, not silent. Any OSError
during the install forces ``ok`` to False — a failed restart launch
must never report as a successful install.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from . import BRIDGE_VERSION
from .logging_setup import redact_text

if TYPE_CHECKING:
    from .config import BridgeSettings
    from .server.rest import ReleaseInfo

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover — cryptography is a pinned dependency
    # None fallback when cryptography is absent (mypy: the names are
    # types in the success branch, hence the ignore codes).
    Ed25519PublicKey = None  # type: ignore[assignment, misc]
    InvalidSignature = Exception  # type: ignore[assignment, misc]

__all__ = [
    "APPLY_SCRIPT_TEMPLATE",
    "ROLLBACK_BACKUP_SUFFIX",
    "ROLLBACK_OBSERVE_SECONDS",
    "ROLLBACK_STATE_FILENAME",
    "START_GRACE_SECONDS",
    "UpdateError",
    "Updater",
    "load_update_public_key",
    "read_rollback_state",
    "rollback_backup_path",
    "verify_artifact",
]

#: Hard cap for a download whose metadata carries no size_bytes.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

#: Rollback (P1-7): the previous-version manifest lives in the
#: staging dir under this name; the backup exe itself lives next to
#: the installed target with ROLLBACK_BACKUP_SUFFIX appended.
ROLLBACK_STATE_FILENAME = "rollback-state.json"
ROLLBACK_BACKUP_SUFFIX = ".previous"

#: Start check: seconds the relaunched process must stay alive
#: before the update counts as installed. A healthy bridge never
#: exits on its own in this window; a crash-on-start (missing DLL,
#: broken bundle) exits in under a second.
START_GRACE_SECONDS = 15

#: Observation window for a relaunched previous version after a
#: rollback, before the result records whether it stayed up.
ROLLBACK_OBSERVE_SECONDS = 5

#: Structured error codes surfaced to the GUI / status endpoint.
ERR_NO_PUBLIC_KEY = "NO_PUBLIC_KEY"
ERR_INVALID_PUBLIC_KEY = "INVALID_PUBLIC_KEY"
ERR_CRYPTO_UNAVAILABLE = "CRYPTO_UNAVAILABLE"
ERR_SIGNATURE_MISSING = "SIGNATURE_MISSING"
ERR_SHA256_MISSING = "SHA256_MISSING"
ERR_SIGNATURE_INVALID = "SIGNATURE_INVALID"
ERR_SHA256_MISMATCH = "SHA256_MISMATCH"
ERR_SIZE_MISMATCH = "SIZE_MISMATCH"
ERR_MIN_VERSION = "MIN_VERSION_NOT_SATISFIED"
ERR_NO_UPDATE = "NO_UPDATE_AVAILABLE"
ERR_NO_DOWNLOAD_URL = "NO_DOWNLOAD_URL"
ERR_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
ERR_CHECK_FAILED = "CHECK_FAILED"
ERR_REST_UNAVAILABLE = "REST_UNAVAILABLE"
ERR_NOT_CONFIGURED = "NOT_CONFIGURED"


class UpdateError(Exception):
    """Structured updater failure — never a raw traceback to the GUI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# version comparison (semver-ish "1.2.3"; non-numeric => unparseable)
# ---------------------------------------------------------------------------
def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    parts: list[int] = []
    for piece in str(version).strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            return None
    return tuple(parts) if parts else None


def _is_newer(candidate: str | None, installed: str) -> bool:
    a, b = _version_tuple(candidate), _version_tuple(installed)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return (a + (0,) * (n - len(a))) > (b + (0,) * (n - len(b)))


def _sanitize_version(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", version) or "unknown"


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file on disk (chunked — the file is exe-sized)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rollback_backup_path(target: str | Path) -> Path:
    """Where the previous version is retained for an install target."""
    return Path(str(target) + ROLLBACK_BACKUP_SUFFIX)


def read_rollback_state(staging_dir: Path | None) -> dict[str, Any] | None:
    """The retained previous version, verified — or None.

    Reads ``rollback-state.json`` from the staging dir and only
    trusts it when the recorded backup file exists AND its SHA-256
    matches the recorded ``previous_sha256``. Anything else (no
    state file, malformed JSON, missing fields, missing backup,
    hash mismatch — including the fresh-install state, which
    records ``previous_version: null``) returns None: no verifiable
    previous version exists, so no fallback launch may be
    attempted. Fail-closed, never silent about it — the caller
    reports ``rollback_available: false``.
    """
    if staging_dir is None:
        return None
    try:
        raw = json.loads((Path(staging_dir) / ROLLBACK_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    previous_version = raw.get("previous_version")
    previous_sha256 = raw.get("previous_sha256")
    previous_path = raw.get("previous_path")
    if not isinstance(previous_version, str) or not previous_version:
        return None
    if not isinstance(previous_sha256, str) or not previous_sha256:
        return None
    if not isinstance(previous_path, str) or not previous_path:
        return None
    backup = Path(previous_path)
    if not backup.is_file():
        return None
    try:
        actual = _sha256_file(backup)
    except OSError:
        return None
    if actual.lower() != previous_sha256.strip().lower():
        logger.warning("[UPDATER] rollback backup {} hash mismatch — refusing to trust it", backup)
        return None
    return raw


# ---------------------------------------------------------------------------
# public key
# ---------------------------------------------------------------------------
def load_update_public_key(settings: BridgeSettings, *, bundled_path: Path | None = None) -> Any | None:
    """Ed25519 verification key, or None when none is configured.

    Precedence: ``settings.update_public_key`` (env
    ``NSE_BRIDGE_UPDATE_PUBLIC_KEY`` / config.json) → bundled
    ``nse_bridge/update_public_key.b64`` file (provisioned by the
    P1-6 signing pipeline; the frozen exe bundles it via
    ``bridge.spec``). Raises :class:`UpdateError` when a key IS
    configured but malformed. Returns None (not an exception) when
    no key is configured — the caller turns that into the structured
    ``NO_PUBLIC_KEY`` refusal. ``bundled_path`` overrides the bundled
    file location (tests only: point it at an absent path to exercise
    the unconfigured case now that the real file ships).
    """
    if Ed25519PublicKey is None:
        raise UpdateError(
            ERR_CRYPTO_UNAVAILABLE,
            "cryptography (Ed25519) is not installed — cannot verify signatures",
        )
    raw_b64 = (getattr(settings, "update_public_key", "") or "").strip()
    if not raw_b64:
        bundled = bundled_path if bundled_path is not None else Path(__file__).with_name("update_public_key.b64")
        if bundled.is_file():
            try:
                raw_b64 = bundled.read_text(encoding="utf-8").strip()
            except OSError:
                raw_b64 = ""
    if not raw_b64:
        return None
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError(
            ERR_INVALID_PUBLIC_KEY,
            "configured update public key is not valid base64",
        ) from exc
    if len(raw) != 32:
        raise UpdateError(
            ERR_INVALID_PUBLIC_KEY,
            f"configured update public key must be 32 raw bytes, got {len(raw)}",
        )
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise UpdateError(ERR_INVALID_PUBLIC_KEY, "configured update public key is not a valid Ed25519 key") from exc


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify_artifact(data: bytes, release: ReleaseInfo, public_key: Any) -> None:
    """Verify downloaded bytes against release metadata.

    Order: size (when ``size_bytes`` is present) → SHA-256 → Ed25519
    signature. Any failure raises :class:`UpdateError`; a null
    signature is unverifiable by definition and refuses. ``public_key``
    must be an Ed25519 public key — callers refuse earlier with
    ``NO_PUBLIC_KEY`` when none is configured.
    """
    if release.signature is None:
        raise UpdateError(
            ERR_SIGNATURE_MISSING,
            "release metadata carries no signature — the release is unverifiable and will not be installed",
        )
    if release.sha256 is None:
        raise UpdateError(
            ERR_SHA256_MISSING,
            "release metadata carries no SHA-256 — the release is unverifiable and will not be installed",
        )
    if release.size_bytes is not None and len(data) != release.size_bytes:
        raise UpdateError(
            ERR_SIZE_MISMATCH,
            f"downloaded size {len(data)} != release size_bytes {release.size_bytes}",
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != release.sha256.strip().lower():
        raise UpdateError(
            ERR_SHA256_MISMATCH,
            "SHA-256 mismatch — the downloaded bytes do not match the release metadata",
        )
    if public_key is None:
        raise UpdateError(
            ERR_NO_PUBLIC_KEY,
            "signature unverifiable — no update public key configured (set NSE_BRIDGE_UPDATE_PUBLIC_KEY)",
        )
    try:
        signature = base64.b64decode(release.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError(ERR_SIGNATURE_INVALID, "release signature is not valid base64") from exc
    try:
        public_key.verify(signature, data)
    except InvalidSignature as exc:
        raise UpdateError(
            ERR_SIGNATURE_INVALID,
            "Ed25519 signature verification failed — the downloaded bytes are not authentic",
        ) from exc


# ---------------------------------------------------------------------------
# restart installer (staged script — our own code, never the artifact)
# ---------------------------------------------------------------------------
#: Self-contained stdlib-only script, staged next to the verified
#: artifact and launched detached when the bridge exits. It waits for
#: the bridge PID to disappear, re-verifies the staged file's SHA-256,
#: then replaces the target (os.replace, copy fallback for
#: cross-device targets) and optionally restarts it. It NEVER executes
#: the staged artifact — it only copies bytes.
APPLY_SCRIPT_TEMPLATE = '''\
"""Staged NSE Bridge restart installer (written by nse_bridge/updates.py).

Self-contained, stdlib-only. Waits for the bridge process to exit,
re-verifies the staged file's SHA-256, then replaces the target exe
(os.replace, shutil.copyfile fallback for cross-device targets) and
optionally restarts it. Never executes the staged artifact.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time


def _pid_gone(pid):
    if sys.platform == "win32":
        return _pid_gone_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _pid_gone_windows(pid):
    """Windows: os.kill(pid, 0) only proves the PID is openable, not
    that the process is alive -- a dead process whose handle is still
    open (e.g. the launcher's Popen object) still opens, so the
    updater waited the full 120s and the CI test timed out at 60s.
    Check the exit code instead: STILL_ACTIVE (259) means running;
    anything else means the process has exited. Any inspection
    failure treats the process as gone: proceeding is safe -- replacing
    a still-running exe fails cleanly with PermissionError below.
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value != 259
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _replace_file(src, dst):
    """Replace dst with src, retrying transient Windows locks.

    A freshly written .exe is often briefly locked by antivirus
    scanning; a single attempt fails with PermissionError and a
    verified update is lost. Retry briefly, then fall back to
    copy+remove (also the cross-device path); a persistent failure
    propagates to the caller's OSError handler.
    """
    for _ in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.2)
        except OSError:
            break
    shutil.copyfile(src, dst)
    os.remove(src)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_executable(path):
    """POSIX: a replaced/restored target must stay executable.

    The staged file is written 0644, so without this a Linux target
    would fail to launch with PermissionError and trigger a needless
    rollback (the same applies to a restored backup, whose copy
    does not preserve the mode). Best-effort: a failure is left for
    the start check to catch, never raised here. No-op on Windows.
    """
    if sys.platform == "win32":
        return False
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o111)
        return True
    except OSError:
        return False


def _write_rollback_state(staging_dir, state):
    out = os.path.join(staging_dir, "rollback-state.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def _backup_previous(target, staging_dir, previous_version, new_version, new_sha256):
    """Retain the running exe + its manifest before replacement (P1-7).

    Copies the current target to ``<target>.previous`` and records
    its version + SHA-256 in rollback-state.json in the staging
    dir. Returns the state dict. A fresh install has no target yet:
    available False, previous_version None -- the install proceeds
    without a backup. Any copy failure raises OSError (after
    retries on transient locks): no backup, no switch -- the caller
    refuses the install.
    """
    backup_path = target + ".previous"
    state = {
        "previous_version": previous_version or None,
        "previous_sha256": None,
        "previous_path": backup_path,
        "target": target,
        "new_version": new_version or None,
        "new_sha256": new_sha256,
        "backed_up_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "available": False,
    }
    if not os.path.isfile(target):
        state["previous_version"] = None
        _write_rollback_state(staging_dir, state)
        return state
    state["previous_sha256"] = _sha256_file(target)
    for _ in range(5):
        try:
            shutil.copy2(target, backup_path)
            break
        except PermissionError:
            time.sleep(0.2)
    else:
        raise OSError("could not back up previous version (locked): %s" % target)
    if _sha256_file(backup_path) != state["previous_sha256"]:
        raise OSError("previous-version backup hash mismatch: %s" % backup_path)
    state["available"] = True
    _write_rollback_state(staging_dir, state)
    return state


def _restore_previous(state):
    """Copy the retained previous exe back over the target.

    Fail-closed: no state, no backup file, or a backup whose SHA-256
    does not match the recorded manifest returns False and touches
    nothing -- unverified bytes are never restored or launched. The
    restored target is re-hashed before reporting success. Never
    raises (it runs inside failure handling); the backup file itself
    is preserved, so the previous version stays recoverable.
    """
    try:
        if not state or not state.get("available"):
            return False
        backup_path = state.get("previous_path")
        target = state.get("target")
        expected = state.get("previous_sha256")
        if not backup_path or not target or not expected:
            return False
        if not os.path.isfile(backup_path):
            return False
        if _sha256_file(backup_path).lower() != str(expected).lower():
            return False
        shutil.copyfile(backup_path, target)
        return _sha256_file(target).lower() == str(expected).lower()
    except OSError:
        return False


def _launch_detached(argv):
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, **kwargs)


def _wait_alive(proc, seconds):
    """True when the process is still running after `seconds`."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return proc.poll() is None


def _fail_start(result, rollback, args, reason):
    """Failed start: restore the previous version and relaunch it.

    Used both when the new version exits inside the grace window
    and when it cannot be launched at all (e.g. WinError 193 on
    Windows -- the staged file is not a valid Win32 application).
    Records the outcome in `result`; never raises (a rollback
    relaunch that itself fails is recorded as
    rollback_launch_failed, not raised).
    """
    result["ok"] = False
    result["error"] = reason
    result["rolled_back"] = True
    restored = _restore_previous(rollback)
    result["rollback_restored"] = restored
    if restored:
        _make_executable(args.target)
        try:
            rproc = _launch_detached([args.restart_cmd])
        except OSError as exc:
            result["rollback_pid"] = None
            result["rollback_process_alive"] = False
            result["rollback_launch_failed"] = True
            result["rollback_error"] = str(exc)
        else:
            result["rollback_pid"] = rproc.pid
            if args.rollback_observe_seconds > 0:
                alive = _wait_alive(rproc, args.rollback_observe_seconds)
            else:
                alive = None
            result["rollback_process_alive"] = alive
            result["rollback_launch_failed"] = alive is False
    else:
        result["rollback_launch_failed"] = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--parent-pid", type=int, required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--restart-cmd", default="")
    ap.add_argument("--previous-version", default="")
    ap.add_argument("--new-version", default="")
    ap.add_argument("--start-grace-seconds", type=int, default=15)
    ap.add_argument("--rollback-observe-seconds", type=int, default=5)
    args = ap.parse_args()
    result = {"ok": False, "error": None, "rolled_back": False, "rollback_available": False}
    staging_dir = os.path.dirname(os.path.abspath(args.staged))
    deadline = time.time() + 120
    while not _pid_gone(args.parent_pid) and time.time() < deadline:
        time.sleep(0.5)
    if not _pid_gone(args.parent_pid):
        result["error"] = "bridge process did not exit within 120s -- update NOT applied"
    else:
        try:
            with open(args.staged, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            if digest.lower() != args.sha256.lower():
                result["error"] = "staged file SHA-256 mismatch -- update NOT applied"
            else:
                # Retention (P1-7): back up the running exe BEFORE
                # replacing it. A backup that cannot be taken refuses
                # the install -- no backup, no switch.
                rollback = _backup_previous(
                    args.target, staging_dir, args.previous_version,
                    args.new_version, digest)
                result["previous_version"] = rollback.get("previous_version")
                result["rollback_available"] = rollback.get("available", False)
                try:
                    _replace_file(args.staged, args.target)
                except OSError:
                    # Never leave a half-written target: restore the backup.
                    _restore_previous(rollback)
                    raise
                result["executable_bit_set"] = _make_executable(args.target)
                result["ok"] = True
                result["target"] = args.target
                if args.restart_cmd:
                    try:
                        proc = _launch_detached([args.restart_cmd])
                    except OSError as exc:
                        # The new version cannot even be launched
                        # (e.g. not a valid Win32 application): that
                        # is a failed start, not a successful install.
                        result["restarted"] = False
                        _fail_start(
                            result, rollback, args,
                            "new version failed to launch (%s) -- rolled back to previous version" % exc)
                    else:
                        result["restarted"] = True
                        result["restarted_pid"] = proc.pid
                        if args.start_grace_seconds > 0:
                            if _wait_alive(proc, args.start_grace_seconds):
                                result["start_check"] = "passed"
                            else:
                                _fail_start(
                                    result, rollback, args,
                                    "new version exited within %ss of start -- "
                                    "rolled back to previous version" % args.start_grace_seconds)
                        else:
                            result["start_check"] = "skipped_no_grace_window"
                else:
                    result["start_check"] = "skipped_no_restart"
        except OSError as exc:
            # An exception anywhere in the install means not-ok, even
            # if ok was already set: a failed restart launch used to
            # report ok=True with an error attached (Windows CI, PR #37).
            result["ok"] = False
            result["error"] = f"install failed: {exc}"
    out = os.path.join(staging_dir, "apply-result.json")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
    except OSError:
        pass
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


# The template is a multi-line string literal, so its line endings are
# the source file's line endings. On a CRLF checkout (Windows CI) the
# literal embeds \r\n; a text-mode write then translates each \n to
# \r\n (\r\n -> \r\r\n) and a universal-newlines read turns \r\r\n
# into \n\n. Normalize once so the template is byte-identical on every
# platform. (The template contains no intentional \r characters.)
# The template is also kept strictly ASCII: it is written as UTF-8 but
# may be read back under the Windows locale default (cp1252), where a
# single non-ASCII character (an em dash did this on run 44272982805)
# decodes to different characters and the template no longer matches.
APPLY_SCRIPT_TEMPLATE = APPLY_SCRIPT_TEMPLATE.replace("\r\n", "\n")


def _resolve_python_executable() -> str | None:
    """Interpreter for running the staged apply script.

    ``sys.executable`` is the running Python — except in the frozen
    PyInstaller build, where it is the bridge .exe itself (Codex P2:
    launching the .exe with a .py script argument re-launches the
    bridge instead of running the script). When frozen, look for a
    system Python on PATH; None when none exists (the caller reports
    it honestly instead of launching garbage).
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# updater
# ---------------------------------------------------------------------------
@dataclass
class _StagedInfo:
    version: str | None = None
    path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


class Updater:
    """Download-only verified updater (CONTRACT v1 §3.5).

    ``rest_client_getter`` is called at check time (the REST client is
    wired at app startup and may be replaced in tests). ``download_transport``
    injects an httpx transport for tests — production downloads go over
    the real network to ``release.download_url`` (the artifact host
    behind the endpoint, per contract §3.5).
    """

    def __init__(
        self,
        settings: BridgeSettings,
        *,
        rest_client_getter: Any = None,
        download_transport: httpx.AsyncBaseTransport | None = None,
        staging_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self._rest_client_getter = rest_client_getter
        self._download_transport = download_transport
        self.staging_dir = staging_dir or (settings.data_dir / "updates")
        self._state = "idle"
        self._latest: ReleaseInfo | None = None
        self._error: UpdateError | None = None
        self._detail = ""
        self._staged = _StagedInfo()
        # Serialize check()/download(): both mutate the same state
        # machine and are reachable from concurrent management calls —
        # two interleaved downloads could double-stage or corrupt the
        # staged-state file. asyncio.Lock (single loop, non-reentrant —
        # download() calls the UNLOCKED internal check).
        self._op_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    @property
    def install_target(self) -> str | None:
        """Exe replaced on restart — only when running frozen (PyInstaller).

        On Linux/dev runs there is no packaged exe to replace, so this
        is None and install-on-restart honestly reports it instead of
        pretending to install.
        """
        if getattr(sys, "frozen", False):
            return sys.executable
        return None

    @property
    def update_public_key_configured(self) -> bool:
        try:
            return load_update_public_key(self.settings) is not None
        except UpdateError:
            # A malformed configured key still counts as "configured" —
            # the error surfaces when an install is attempted.
            return bool((getattr(self.settings, "update_public_key", "") or "").strip())

    def _set_error(self, err: UpdateError, state: str = "error") -> None:
        self._state = state
        self._error = err
        self._detail = err.message
        logger.warning("[UPDATER] {}: {}", err.code, redact_text(err.message))

    def _latest_dict(self) -> dict[str, Any] | None:
        rel = self._latest
        if rel is None:
            return None
        return {
            "version": rel.version,
            "download_url": rel.download_url,
            "sha256": rel.sha256,
            "size_bytes": rel.size_bytes,
            "min_connector_version": rel.min_connector_version,
            "signature_present": bool(rel.signature),
            "released_at": rel.released_at,
        }

    def _load_staged_state(self) -> _StagedInfo | None:
        """Staged update that survived a restart (from update-state.json).

        Only the recorded size is re-checked here — the apply script
        re-verifies the full SHA-256 before any replacement, so status
        polling does not re-hash a large artifact on every call.
        """
        state_file = self.staging_dir / "update-state.json"
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        path = raw.get("staged_path")
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        size = raw.get("size_bytes")
        if isinstance(size, int) and Path(path).stat().st_size != size:
            return None
        return _StagedInfo(
            version=raw.get("version"),
            path=path,
            sha256=raw.get("sha256"),
            size_bytes=size if isinstance(size, int) else None,
        )

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Current updater status (safe to poll from the GUI)."""
        staged = self._staged
        state = self._state
        if not staged.path:
            persisted = self._load_staged_state()
            if persisted is not None:
                staged = persisted
                if state in ("idle", "up_to_date", "update_available"):
                    state = "staged"
        rollback = read_rollback_state(self.staging_dir)
        return {
            "installed_version": BRIDGE_VERSION,
            "state": state,
            "latest": self._latest_dict(),
            "update_available": state == "update_available",
            "staged_version": staged.version,
            "staged_path": staged.path,
            "staged_sha256": staged.sha256,
            "verified": bool(staged.path) and state in ("staged",),
            "restart_required": bool(staged.path),
            "install_target": self.install_target,
            "update_public_key_configured": self.update_public_key_configured,
            # Rollback (P1-7): the retained previous version, only
            # reported when its backup exists and matches its
            # manifest — a tampered or missing backup reports false
            # (fail-closed: it is never launched).
            "rollback_available": rollback is not None,
            "previous_version": rollback["previous_version"] if rollback else None,
            "previous_sha256": rollback["previous_sha256"] if rollback else None,
            "error": self._error.to_dict() if self._error else None,
            "detail": self._detail,
        }

    # ------------------------------------------------------------------
    async def check(self) -> dict[str, Any]:
        """Poll releases/latest; report availability / incompatibility.

        Serialized against download() (see _op_lock). Never downloads
        anything. Mirrors the GET /updates/latest semantics: no server
        client → ``unavailable``; no API key → ``not_configured``;
        server failure → ``error``/``CHECK_FAILED``.
        """
        async with self._op_lock:
            return await self._check_locked()

    async def _check_locked(self) -> dict[str, Any]:
        self._state = "checking"
        self._error = None
        self._detail = "checking for updates…"
        rest = self._rest_client_getter() if self._rest_client_getter else None
        if rest is None:
            self._set_error(
                UpdateError(ERR_REST_UNAVAILABLE, "Server client not initialised yet — the bridge is still starting."),
                state="unavailable",
            )
            return self.status()
        if not self.settings.server_token:
            self._set_error(
                UpdateError(ERR_NOT_CONFIGURED, "No API key configured — set it in Settings first, then check again."),
                state="not_configured",
            )
            return self.status()
        try:
            rel = await rest.releases_latest()
        except Exception as exc:  # noqa: BLE001 — any REST failure is a check failure, not a crash
            code = getattr(getattr(exc, "code", None), "value", None) or ERR_CHECK_FAILED
            self._set_error(UpdateError(code, f"Update check failed: {redact_text(str(exc))[:300]}"))
            return self.status()
        self._latest = rel
        installed = BRIDGE_VERSION
        if rel.version is None:
            self._state = "unknown"
            self._detail = "The server answered but reported no version."
            return self.status()
        if rel.min_connector_version and _is_newer(rel.min_connector_version, installed):
            self._set_error(
                UpdateError(
                    ERR_MIN_VERSION,
                    f"Release {rel.version} requires connector >= {rel.min_connector_version} "
                    f"(installed {installed}) — update refused",
                ),
                state="incompatible",
            )
            return self.status()
        if _is_newer(rel.version, installed):
            self._state = "update_available"
            self._detail = f"Version {rel.version} is available (installed {installed})."
        else:
            self._state = "up_to_date"
            self._detail = f"Installed {installed} is the latest known version."
        return self.status()

    # ------------------------------------------------------------------
    async def download(self) -> dict[str, Any]:
        """Download + verify + stage the latest release.

        Serialized against check()/download() (see _op_lock). Refuses
        (structured error, nothing staged) when: no update was found,
        the release needs a newer minimum connector version, no public
        key is configured, the signature/SHA-256 is missing, or
        verification fails. A failed download never leaves a staged file.
        """
        async with self._op_lock:
            return await self._download_locked()

    async def _download_locked(self) -> dict[str, Any]:
        if self._latest is None:
            await self._check_locked()
        if self._state == "incompatible":
            return self.status()  # error already recorded by check()
        if self._state not in ("update_available", "staged"):
            if self._state in ("up_to_date",):
                self._set_error(
                    UpdateError(ERR_NO_UPDATE, "No update available to download."),
                )
            elif self._error is None:
                self._set_error(
                    UpdateError(ERR_CHECK_FAILED, "No release information — run an update check first."),
                )
            return self.status()
        rel = self._latest
        assert rel is not None  # check() above guarantees it
        try:
            public_key = load_update_public_key(self.settings)
        except UpdateError as exc:
            self._set_error(exc)
            return self.status()
        if public_key is None:
            self._set_error(
                UpdateError(
                    ERR_NO_PUBLIC_KEY,
                    "signature unverifiable — no update public key configured "
                    "(set NSE_BRIDGE_UPDATE_PUBLIC_KEY to the NSE release-signing public key); "
                    "refusing to install an unverifiable binary",
                )
            )
            return self.status()
        if rel.signature is None:
            self._set_error(
                UpdateError(
                    ERR_SIGNATURE_MISSING,
                    "release metadata carries no signature — the release is unverifiable and will not be installed",
                )
            )
            return self.status()
        if rel.sha256 is None:
            self._set_error(
                UpdateError(
                    ERR_SHA256_MISSING,
                    "release metadata carries no SHA-256 — the release is unverifiable and will not be installed",
                )
            )
            return self.status()
        if not rel.download_url:
            self._set_error(UpdateError(ERR_NO_DOWNLOAD_URL, "release metadata carries no download_url"))
            return self.status()
        self._state = "downloading"
        self._error = None
        self._detail = f"downloading {rel.version}…"
        try:
            data = await self._download_bytes(rel.download_url, rel.size_bytes)
        except UpdateError as exc:
            self._set_error(exc)
            return self.status()
        except Exception as exc:  # noqa: BLE001 — transport failure is a download failure
            self._set_error(UpdateError(ERR_DOWNLOAD_FAILED, f"Download failed: {redact_text(str(exc))[:300]}"))
            return self.status()
        try:
            verify_artifact(data, rel, public_key)
        except UpdateError as exc:
            self._set_error(exc)
            return self.status()
        self._stage(data, rel)
        self._state = "staged"
        self._error = None
        self._detail = (
            f"Version {rel.version} downloaded and verified (SHA-256 + Ed25519) — it will install on restart."
        )
        logger.info("[UPDATER] staged verified release {} ({} bytes)", rel.version, len(data))
        return self.status()

    async def _download_bytes(self, url: str, size_bytes: int | None) -> bytes:
        """Stream the artifact with a hard size cap (never unbounded)."""
        cap = size_bytes if size_bytes is not None else MAX_DOWNLOAD_BYTES
        chunks: list[bytes] = []
        received = 0
        async with (
            httpx.AsyncClient(
                transport=self._download_transport,
                trust_env=False,
                follow_redirects=True,
                timeout=httpx.Timeout(120.0),
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code != 200:
                raise UpdateError(ERR_DOWNLOAD_FAILED, f"download URL answered HTTP {resp.status_code}")
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                received += len(chunk)
                if received > cap:
                    raise UpdateError(
                        ERR_SIZE_MISMATCH,
                        f"download exceeded expected size ({received} > {cap} bytes) — aborted",
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    # ------------------------------------------------------------------
    def _stage(self, data: bytes, rel: ReleaseInfo) -> Path:
        """Atomically stage verified bytes + state + restart script."""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        version = _sanitize_version(rel.version or "unknown")
        final = self.staging_dir / f"nse-bridge-{version}.exe"
        self._prune_stale_staged_artifacts(keep=final)
        tmp = self.staging_dir / f".{final.name}.tmp-{os.getpid()}"
        tmp.write_bytes(data)
        os.replace(tmp, final)
        self._staged = _StagedInfo(
            version=rel.version,
            path=str(final),
            sha256=rel.sha256,
            size_bytes=len(data),
        )
        state_file = self.staging_dir / "update-state.json"
        state_file.write_text(
            json.dumps(
                {
                    "version": rel.version,
                    "staged_path": str(final),
                    "sha256": rel.sha256,
                    "size_bytes": len(data),
                    "signature_verified": True,
                    # The version being replaced — the apply script
                    # records it as the previous version when it
                    # backs the running exe up (P1-7 rollback).
                    "installed_version": BRIDGE_VERSION,
                    "staged_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.write_apply_script()
        return final

    def _prune_stale_staged_artifacts(self, *, keep: Path) -> None:
        """Remove previously staged exes + temp files (each verified
        download used to accumulate a full exe per version — every
        staged artifact except the incoming one is obsolete, and a
        stale one is NEVER installed: the apply script only ever copies
        the file recorded in update-state.json, which this call
        invalidates below via the fresh state write)."""
        try:
            for entry in self.staging_dir.iterdir():
                if entry.is_dir():
                    continue
                if entry == keep:
                    continue
                if entry.name == "update-state.json" or entry.name.startswith("apply_update"):
                    continue
                if entry.name.startswith("nse-bridge-") and (entry.name.endswith(".exe") or ".tmp-" in entry.name):
                    entry.unlink(missing_ok=True)
                    logger.info("[UPDATER] pruned stale staged artifact {}", entry.name)
        except OSError as exc:  # pruning is housekeeping, never fatal
            logger.debug("[UPDATER] staged-artifact pruning skipped: {}", type(exc).__name__)

    def write_apply_script(self) -> Path:
        """Write the staged restart-installer script (our own code).

        The script is inert until launched on bridge exit; it only
        copies the already-verified staged bytes and never executes them.
        """
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        script = self.staging_dir / "apply_update.py"
        # newline="": write the template byte-exact. The default text-mode
        # translation would turn the template's \n into \r\n on Windows,
        # so the staged file would not round-trip to the template.
        script.write_text(APPLY_SCRIPT_TEMPLATE, encoding="utf-8", newline="")
        return script

    def launch_restart_installer(self) -> dict[str, Any]:
        """Launch the staged installer detached (bridge-exit hook).

        Only meaningful when a verified update is staged AND the bridge
        runs frozen (a packaged exe target exists). On Linux/dev runs
        this honestly reports ``not_frozen_no_target`` — no pretend install.
        The caller (app lifespan teardown) invokes this best-effort on shutdown.
        """
        staged = self._staged.path
        if not staged:
            persisted = self._load_staged_state()
            staged = persisted.path if persisted else None
        if not staged:
            return {"launched": False, "reason": "no_staged_update"}
        target = self.install_target
        if target is None:
            return {
                "launched": False,
                "reason": "not_frozen_no_target",
                "detail": (
                    "a verified update is staged, but this is not a packaged exe run — "
                    "restart install only applies to the frozen Windows exe (unverified on Linux)"
                ),
            }
        script = self.write_apply_script()
        staged_state = self._staged if self._staged.path else (self._load_staged_state() or _StagedInfo())
        interpreter = _resolve_python_executable()
        if interpreter is None:
            return {
                "launched": False,
                "reason": "no_python_interpreter",
                "detail": (
                    "frozen build has no Python interpreter to run the apply script: "
                    "install a system Python (python / python3 / py on PATH) and retry"
                ),
            }
        cmd = [
            interpreter,
            str(script),
            "--staged",
            str(staged),
            "--target",
            target,
            "--parent-pid",
            str(os.getpid()),
            "--sha256",
            staged_state.sha256 or "",
            # Rollback (P1-7): the apply script backs the running
            # exe up as the previous version before replacing it,
            # then watches the new process and falls back on a
            # failed start.
            "--previous-version",
            BRIDGE_VERSION,
            "--new-version",
            staged_state.version or "",
            "--start-grace-seconds",
            str(START_GRACE_SECONDS),
            "--rollback-observe-seconds",
            str(ROLLBACK_OBSERVE_SECONDS),
            "--restart-cmd",
            target,
        ]
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            # No shell: argv is a list of our own values (this
            # interpreter + our staged apply script + paths/flags),
            # never untrusted input. This is the designed restart-installer launch.
            subprocess.Popen(cmd, **kwargs)  # nosec B603
        except OSError as exc:
            return {"launched": False, "reason": "launch_failed", "detail": str(exc)}
        logger.info("[UPDATER] restart installer launched for staged {}", staged_state.version)
        return {"launched": True, "staged": str(staged), "target": target}

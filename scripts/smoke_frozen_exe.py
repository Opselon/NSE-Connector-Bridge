"""Frozen-exe smoke test (Python; mirrors scripts/smoke_exe_local.ps1).

Usage (Windows, after ``pyinstaller bridge.spec``)::

    python scripts/smoke_frozen_exe.py [--exe dist/NSE-MT5-Bridge.exe] [--port 8799]

What it proves about the frozen exe — the v1.3.0 visible-startup
contract, from the outside, with no MT5 terminal:

1. ``--help`` exits 0 (the exe starts at all).
2. The exe serves ``/health`` within 90 s (cold onefile extraction +
   Defender scan budget) and ``/ui`` + ``/docs`` answer 200.
3. The machine-readable CLI works against the RUNNING instance:
   ``status --json``, ``health --json``, ``mt5 list --json`` exit 0
   and parse as JSON.
4. A second launch while the first is running exits 0 promptly —
   the bring-to-front path (open the running dashboard) — instead
   of hanging, crashing, or starting a second server.

What it does NOT prove (no real MT5 on a build machine): MT5
attach, platform enrolment, the native pywebview window, or the
tray icon — those need a Windows desktop with MT5 (manual E2E).
The second-launch step DOES open the default browser (that is the
bring-to-front behaviour); on a headless runner the explicit
Edge/Chrome fallback is what gets exercised.

Exit code 0 = all checks passed; non-zero + a message = failure.
Stdlib-only so the release job can run it with the build Python.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Cold-start budget: PyInstaller onefile extraction + first-launch
#: Defender scan of a fresh unsigned exe measured >10 s on hosted
#: Windows runners; the bridge itself allows 90 s.
STARTUP_TIMEOUT_S = 90.0
#: A second launch must exit (bring-to-front) rather than hang; the
#: browser open + dialog path is bounded by this.
SECOND_LAUNCH_TIMEOUT_S = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(url: str, timeout_s: float = 15.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def _wait_ready(base_url: str, timeout_s: float) -> float:
    started = time.monotonic()
    while True:
        try:
            status, _ = _http_get(f"{base_url}/health", timeout_s=2.0)
            if status == 200:
                return time.monotonic() - started
        except RuntimeError:
            pass
        if time.monotonic() - started > timeout_s:
            raise RuntimeError(f"no /health 200 within {timeout_s:.0f}s")
        time.sleep(0.3)


def _run_cli(exe: str, args: list[str]) -> dict:
    proc = subprocess.run(
        [exe, *args, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"CLI {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CLI {' '.join(args)} did not print JSON: {proc.stdout[:500]!r}") from exc
    if not isinstance(doc, dict):
        raise TypeError(f"CLI {' '.join(args)} printed a non-object: {doc!r}")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description="Frozen-exe smoke test for NSE-MT5-Bridge.")
    parser.add_argument("--exe", default="dist/NSE-MT5-Bridge.exe", help="path to the frozen exe")
    parser.add_argument("--port", type=int, default=0, help="port to serve on (0 = pick a free one)")
    args = parser.parse_args()

    exe = str(Path(args.exe))
    port = args.port or _free_port()
    base_url = f"http://127.0.0.1:{port}"
    failures: list[str] = []

    def check(name: str, fn) -> None:  # type: ignore[no-untyped-def]
        try:
            fn()
            print(f"  ok: {name}")
        except Exception as exc:  # noqa: BLE001 — one failed check must not abort the rest
            failures.append(f"{name}: {exc}")
            print(f"  FAIL: {name}: {exc}")

    print(f"smoke: exe={exe} port={port}")

    def _help() -> None:
        proc = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"--help exited {proc.returncode}: {proc.stderr.strip()[:300]}")

    check("--help exits 0", _help)
    if failures:
        print("smoke FAILED")
        return 1

    proc = subprocess.Popen(
        [exe, "--gui", "none", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready_s = _wait_ready(base_url, STARTUP_TIMEOUT_S)
        print(f"  ok: startup-to-ready: {ready_s:.1f}s")

        def _ui() -> None:
            status, body = _http_get(f"{base_url}/ui")
            if status != 200:
                raise RuntimeError(f"/ui HTTP {status}")
            if b"NSE Bridge" not in body:
                raise RuntimeError("/ui body does not contain the Control Center")
            print(f"  ok: /ui HTTP 200, {len(body)} bytes")

        def _docs() -> None:
            status, _ = _http_get(f"{base_url}/docs")
            if status != 200:
                raise RuntimeError(f"/docs HTTP {status}")

        check("/ui answers 200", _ui)
        check("/docs answers 200", _docs)

        for cli_args in (["status"], ["health"], ["mt5", "list"]):
            name = "CLI " + " ".join(cli_args) + " --json"
            check(name, lambda a=cli_args: _run_cli(exe, a))

        def _second_launch() -> None:
            try:
                second = subprocess.run(
                    [exe, "--gui", "none", "--port", str(port)],
                    capture_output=True,
                    text=True,
                    timeout=SECOND_LAUNCH_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"second launch did not exit within {SECOND_LAUNCH_TIMEOUT_S:.0f}s") from None
            if second.returncode != 0:
                raise RuntimeError(
                    f"second launch exited {second.returncode} (expected 0 = bring-to-front): "
                    f"{second.stderr.strip()[:300]}"
                )

        check("second launch exits 0 (bring-to-front)", _second_launch)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    if failures:
        print(f"smoke FAILED ({len(failures)} check(s))")
        return 1
    print("smoke PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

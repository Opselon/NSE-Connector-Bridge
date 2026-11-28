"""Unit tests for scripts/sign_release_artifact.py (P1-6 signing pipeline).

The script is exercised as a subprocess (like
test_write_release_manifest.py): sign with a fresh throwaway keypair
(never the real release key), verify the output with the production
verifier ``nse_bridge.updates.verify_artifact``, and prove the
failure modes — tampered bytes, wrong key, missing key, malformed
key — refuse without ever echoing key material.
Label: unit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nse_bridge.server.rest import ReleaseInfo
from nse_bridge.updates import UpdateError, load_update_public_key, verify_artifact

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sign_release_artifact.py"
MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "write_release_manifest.py"
SIGNING_KEY_ENV = "NSE_BRIDGE_RELEASE_SIGNING_KEY"

ARTIFACT = b"NSE-BRIDGE-FAKE-ARTIFACT-v99.99.99" + b"\x00" * 64


def _make_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    seed_b64 = base64.b64encode(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return priv, pem, seed_b64, pub_b64


def _public_key(pub_b64):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))


def _run(*args: str, env_key: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != SIGNING_KEY_ENV}
    if env_key is not None:
        env[SIGNING_KEY_ENV] = env_key
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_sign_writes_sig_sidecar_verifiable_by_updates(tmp_path: Path):
    _, pem, _, pub_b64 = _make_keypair()
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run(str(exe), env_key=pem)
    assert proc.returncode == 0, proc.stderr
    # The key itself never appears in the script's output.
    assert pem not in proc.stdout and pem not in proc.stderr
    sig_path = tmp_path / "NSE-MT5-Bridge.exe.sig"
    assert sig_path.is_file()
    sig_b64 = sig_path.read_text(encoding="utf-8").strip()
    assert len(base64.b64decode(sig_b64, validate=True)) == 64
    # The production verifier accepts the script's signature...
    release = ReleaseInfo(
        version="99.99.99",
        download_url="https://example.invalid/bridge.exe",
        sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        size_bytes=len(ARTIFACT),
        min_connector_version="1.0.0",
        signature=sig_b64,
        released_at="2026-11-18T00:00:00Z",
    )
    verify_artifact(ARTIFACT, release, _public_key(pub_b64))
    # ...and refuses tampered bytes signed by the same key.
    tampered = bytes([ARTIFACT[0] ^ 0xFF]) + ARTIFACT[1:]
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(tampered, release, _public_key(pub_b64))
    assert excinfo.value.code in ("SHA256_MISMATCH", "SIGNATURE_INVALID")


def test_sign_accepts_base64_seed_key(tmp_path: Path):
    _, _, seed_b64, pub_b64 = _make_keypair()
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run(str(exe), env_key=seed_b64)
    assert proc.returncode == 0, proc.stderr
    sig_b64 = (tmp_path / "NSE-MT5-Bridge.exe.sig").read_text(encoding="utf-8").strip()
    release = ReleaseInfo(
        version="99.99.99",
        download_url="https://example.invalid/bridge.exe",
        sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        size_bytes=len(ARTIFACT),
        min_connector_version="1.0.0",
        signature=sig_b64,
        released_at="2026-11-18T00:00:00Z",
    )
    verify_artifact(ARTIFACT, release, _public_key(pub_b64))


def test_sign_accepts_key_file(tmp_path: Path):
    _, pem, _, pub_b64 = _make_keypair()
    key_file = tmp_path / "signing-key.pem"
    key_file.write_text(pem, encoding="utf-8")
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run(str(exe), "--key-file", str(key_file))
    assert proc.returncode == 0, proc.stderr
    sig_b64 = (tmp_path / "NSE-MT5-Bridge.exe.sig").read_text(encoding="utf-8").strip()
    release = ReleaseInfo(
        version="99.99.99",
        download_url="https://example.invalid/bridge.exe",
        sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        size_bytes=len(ARTIFACT),
        min_connector_version="1.0.0",
        signature=sig_b64,
        released_at="2026-11-18T00:00:00Z",
    )
    verify_artifact(ARTIFACT, release, _public_key(pub_b64))


def test_verify_mode_accepts_valid_and_refuses_tampered(tmp_path: Path):
    _, pem, _, pub_b64 = _make_keypair()
    pub_file = tmp_path / "pub.b64"
    pub_file.write_text(pub_b64 + "\n", encoding="utf-8")
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    assert _run(str(exe), env_key=pem).returncode == 0
    assert _run("--verify", "--public-key-file", str(pub_file), str(exe)).returncode == 0
    exe.write_bytes(bytes([ARTIFACT[0] ^ 0xFF]) + ARTIFACT[1:])
    proc = _run("--verify", "--public-key-file", str(pub_file), str(exe))
    assert proc.returncode == 1
    assert "verification failed" in proc.stderr


def test_verify_mode_refuses_missing_sig_sidecar(tmp_path: Path):
    _, _, _, pub_b64 = _make_keypair()
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run("--verify", "--public-key-b64", pub_b64, str(exe))
    assert proc.returncode == 1
    assert "signature sidecar not found" in proc.stderr


def test_verify_mode_checks_manifest_sha256(tmp_path: Path):
    _, pem, _, pub_b64 = _make_keypair()
    pub_file = tmp_path / "pub.b64"
    pub_file.write_text(pub_b64 + "\n", encoding="utf-8")
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    out = tmp_path / "out"
    assert _run(str(exe), env_key=pem).returncode == 0
    sig = tmp_path / "NSE-MT5-Bridge.exe.sig"
    manifest_proc = subprocess.run(
        [
            sys.executable,
            str(MANIFEST_SCRIPT),
            str(exe),
            "--tag",
            "v99.99.99",
            "--out-dir",
            str(out),
            "--signature",
            f"NSE-MT5-Bridge.exe:{sig}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert manifest_proc.returncode == 0, manifest_proc.stderr
    manifest = out / "release-manifest.json"
    assert json.loads(manifest.read_text())["files"][0]["signature"]
    assert (
        _run(
            "--verify",
            "--public-key-file",
            str(pub_file),
            "--manifest",
            str(manifest),
            str(exe),
        ).returncode
        == 0
    )
    # A manifest whose SHA-256 no longer matches the file refuses.
    doc = json.loads(manifest.read_text())
    doc["files"][0]["sha256"] = "00" * 32
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    proc = _run(
        "--verify",
        "--public-key-file",
        str(pub_file),
        "--manifest",
        str(manifest),
        str(exe),
    )
    assert proc.returncode == 1
    assert "SHA-256 mismatch" in proc.stderr


def test_sign_without_key_fails_without_leaking(tmp_path: Path):
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run(str(exe))
    assert proc.returncode == 2
    assert "NSE_BRIDGE_RELEASE_SIGNING_KEY" in proc.stderr
    assert not (tmp_path / "NSE-MT5-Bridge.exe.sig").exists()


def test_sign_malformed_key_fails_without_echoing_it(tmp_path: Path):
    # A pasted secret that turns out malformed must never come back
    # in the script's output (CI logs keep it).
    fake_secret = "-----BEGIN PRIVATE KEY-----\nTHIS-IS-NOT-A-REAL-KEY-BUT-MUST-NOT-LEAK\n-----END PRIVATE KEY-----"
    exe = _write(tmp_path / "NSE-MT5-Bridge.exe", ARTIFACT)
    proc = _run(str(exe), env_key=fake_secret)
    assert proc.returncode == 2
    assert "THIS-IS-NOT-A-REAL-KEY-BUT-MUST-NOT-LEAK" not in proc.stdout
    assert "THIS-IS-NOT-A-REAL-KEY-BUT-MUST-NOT-LEAK" not in proc.stderr
    assert not (tmp_path / "NSE-MT5-Bridge.exe.sig").exists()


def test_show_public_key_matches_keypair(tmp_path: Path):
    _, pem, _, pub_b64 = _make_keypair()
    proc = _run("--show-public-key", env_key=pem)
    assert proc.returncode == 0, proc.stderr
    assert f"public_key_b64: {pub_b64}" in proc.stdout
    assert "PRIVATE KEY" not in proc.stdout
    assert pem not in proc.stdout


def test_sign_missing_artifact_fails(tmp_path: Path):
    _, pem, _, _ = _make_keypair()
    proc = _run(str(tmp_path / "does-not-exist.exe"), env_key=pem)
    assert proc.returncode == 2
    assert "not found" in proc.stderr


def test_bundled_public_key_rejects_unrelated_signature(tmp_path: Path, monkeypatch):
    # Negative proof the bundled key is enforced: a signature from
    # any other key fails against it (the positive round-trip with
    # the real private key is proven out-of-band at key handover —
    # the private key is never in this repo).
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    priv, _, _, _ = _make_keypair()
    sig_b64 = base64.b64encode(priv.sign(ARTIFACT)).decode()
    release = ReleaseInfo(
        version="99.99.99",
        download_url="https://example.invalid/bridge.exe",
        sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        size_bytes=len(ARTIFACT),
        min_connector_version="1.0.0",
        signature=sig_b64,
        released_at="2026-11-18T00:00:00Z",
    )
    from nse_bridge.config import BridgeSettings

    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    bundled_key = load_update_public_key(settings)
    assert bundled_key is not None
    assert bundled_key.public_bytes(Encoding.Raw, PublicFormat.Raw) == base64.b64decode(
        (REPO_ROOT / "nse_bridge" / "update_public_key.b64").read_text(encoding="utf-8").strip()
    )
    with pytest.raises(UpdateError) as excinfo:
        verify_artifact(ARTIFACT, release, bundled_key)
    assert excinfo.value.code == "SIGNATURE_INVALID"

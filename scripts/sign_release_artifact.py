"""Sign release artifacts with Ed25519 (P1-6 / P5b signing pipeline).

The Bridge updater verifies a release BEFORE any install
(``nse_bridge/updates.py: verify_artifact``): the signature is a
base64 Ed25519 signature over the raw artifact bytes, and the public
key is base64 raw 32-byte material (env ``NSE_BRIDGE_UPDATE_PUBLIC_KEY``
or the bundled ``nse_bridge/update_public_key.b64``). This script is
the signing side of that contract — it produces exactly the signatures
the verifier accepts, and nothing else. It never invents a key.

Usage (sign; the release workflow runs this on tags):
  python scripts/sign_release_artifact.py <artifact> [<artifact> ...]
      [--key-file <private-key-pem>]

  Private key source (first wins): ``--key-file`` (local/operator use),
  else env ``NSE_BRIDGE_RELEASE_SIGNING_KEY`` (the GitHub Actions
  secret the release job reads). The env/secret value is EITHER a PEM
  PKCS#8 Ed25519 private key (what the operator backs up to FTP
  ``/opselon-passwords/nse-bridge-release-signing-private-key.pem``)
  OR base64 raw 32-byte seed — the same two forms ``load_pem`` /
  ``from_private_bytes`` accept. The key is NEVER printed, logged, or
  written anywhere by this script; output shows only artifact names,
  SHA-256 prefixes, and signature prefixes.

  Each artifact gains a ``<artifact>.sig`` sidecar holding the base64
  signature (one line). ``scripts/write_release_manifest.py
  --signature <name>:<sigfile>`` then attaches the signatures to the
  release manifest, and the Platform serves the setup installer's
  signature from its ``.sig`` sidecar via ``releases/latest`` (D19).

Usage (verify — the same checks ``verify_artifact`` performs, in the
same order: SHA-256 when a manifest is given, then Ed25519):
  python scripts/sign_release_artifact.py --verify
      --public-key-file nse_bridge/update_public_key.b64
      [--manifest dist/release-manifest.json]
      <artifact> [<artifact> ...]

  Reads each ``<artifact>.sig`` and verifies it against the file
  bytes. This intentionally uses only ``cryptography`` + ``hashlib``
  (no ``nse_bridge`` import) so the release job can run it after a
  minimal ``pip install cryptography``; the unit tests tie the output
  to ``verify_artifact`` itself (sign here, verify there).

Usage (operator aid):
  python scripts/sign_release_artifact.py --show-public-key
      [--key-file <private-key-pem>]

  Prints the base64 public key for the signing key (PUBLIC material —
  safe to log) so the operator can confirm the secret's keypair
  matches the bundled ``update_public_key.b64`` without exposing
  the private key.

Exit codes: 0 ok · 1 verification failed · 2 usage / key / file error.
Only dependency: ``cryptography`` (pinned in requirements.txt).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SIGNING_KEY_ENV = "NSE_BRIDGE_RELEASE_SIGNING_KEY"
#: An Ed25519 signature is always 64 raw bytes (88 base64 chars).
SIGNATURE_BYTES = 64

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )
except ImportError:  # pragma: no cover — cryptography is a pinned dependency
    Ed25519PrivateKey = None  # type: ignore[assignment, misc]
    Ed25519PublicKey = None  # type: ignore[assignment, misc]
    InvalidSignature = Exception  # type: ignore[assignment, misc]
    load_pem_private_key = None  # type: ignore[assignment]


def _fail(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def load_signing_key(raw: str):
    """Ed25519 private key from PEM text or base64 raw 32-byte seed.

    ``raw`` is never echoed back in any error message — a malformed
    key is reported by shape only, so a pasted secret cannot leak
    into CI logs through this script.
    """
    if Ed25519PrivateKey is None:
        raise RuntimeError("cryptography is not installed — cannot sign")
    text = raw.strip()
    if not text:
        raise ValueError("empty signing key")
    if "-----BEGIN" in text:
        # Tolerate a one-line PEM whose newlines arrived escaped
        # (e.g. a secret pasted with literal "\\n").
        if "\n" not in text and "\\n" in text:
            text = text.replace("\\n", "\n")
        try:
            key = load_pem_private_key(text.encode("utf-8"), password=None)
        except ValueError as exc:
            raise ValueError("signing key is not a valid PEM Ed25519 private key") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("signing key PEM is not an Ed25519 private key")
        return key
    try:
        seed = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signing key is neither PEM nor valid base64") from exc
    if len(seed) != 32:
        raise ValueError(f"signing key seed must be 32 raw bytes, got {len(seed)}")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_b64(private_key) -> str:
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _load_public_key(*, b64: str | None, path: Path | None) -> bytes:
    if b64 is not None and path is not None:
        raise ValueError("pass exactly one of --public-key-b64 / --public-key-file")
    if b64 is not None:
        text = b64.strip()
    elif path is not None:
        if not path.is_file():
            raise ValueError(f"public key file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
    else:
        raise ValueError("verify needs --public-key-b64 or --public-key-file")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("public key is not valid base64") from exc
    if len(raw) != 32:
        raise ValueError(f"public key must be 32 raw bytes, got {len(raw)}")
    return raw


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(manifest_path: Path) -> dict[str, str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read manifest {manifest_path}: {exc}") from exc
    out: dict[str, str] = {}
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and entry.get("name") and entry.get("sha256"):
            out[str(entry["name"])] = str(entry["sha256"]).lower()
    return out


def _read_signature(artifact: Path) -> bytes:
    # Sidecar name is "<artifact name>.sig" (e.g. "NSE-MT5-Bridge.exe.sig").
    sig_path = artifact.parent / f"{artifact.name}.sig"
    if not sig_path.is_file():
        raise ValueError(f"signature sidecar not found: {sig_path}")
    text = sig_path.read_text(encoding="utf-8").strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"signature in {sig_path} is not valid base64") from exc
    if len(raw) != SIGNATURE_BYTES:
        raise ValueError(f"signature in {sig_path} must be {SIGNATURE_BYTES} bytes, got {len(raw)}")
    return raw


def _sign_artifacts(private_key, artifacts: list[Path]) -> int:
    for artifact in artifacts:
        if not artifact.is_file():
            return _fail(f"artifact not found: {artifact}")
        data = artifact.read_bytes()
        signature = base64.b64encode(private_key.sign(data)).decode("ascii")
        sig_path = artifact.parent / f"{artifact.name}.sig"
        sig_path.write_text(signature + "\n", encoding="utf-8")
        digest = hashlib.sha256(data).hexdigest()
        print(f"signed {artifact.name} (sha256 {digest[:16]}…, sig {signature[:16]}…)")
    return 0


def _verify_artifacts(public_raw: bytes, artifacts: list[Path], manifest: Path | None) -> int:
    public_key = Ed25519PublicKey.from_public_bytes(public_raw)
    expected_sha = _manifest_sha256(manifest) if manifest is not None else {}
    failed = False
    for artifact in artifacts:
        if not artifact.is_file():
            print(f"error: artifact not found: {artifact}", file=sys.stderr)
            failed = True
            continue
        data = artifact.read_bytes()
        if artifact.name in expected_sha:
            digest = hashlib.sha256(data).hexdigest()
            if digest.lower() != expected_sha[artifact.name]:
                print(
                    f"FAIL {artifact.name}: SHA-256 mismatch (manifest "
                    f"{expected_sha[artifact.name][:16]}… != actual {digest[:16]}…)",
                    file=sys.stderr,
                )
                failed = True
                continue
        try:
            signature = _read_signature(artifact)
        except ValueError as exc:
            print(f"FAIL {artifact.name}: {exc}", file=sys.stderr)
            failed = True
            continue
        try:
            public_key.verify(signature, data)
        except InvalidSignature:
            print(f"FAIL {artifact.name}: Ed25519 signature verification failed", file=sys.stderr)
            failed = True
            continue
        print(f"verified {artifact.name} (Ed25519 OK)")
    return 1 if failed else 0


def _key_source(key_file: Path | None) -> str:
    if key_file is not None:
        if not key_file.is_file():
            raise ValueError(f"key file not found: {key_file}")
        return key_file.read_text(encoding="utf-8")
    env_value = os.environ.get(SIGNING_KEY_ENV, "")
    if not env_value.strip():
        raise ValueError(
            f"no signing key: pass --key-file or set {SIGNING_KEY_ENV} "
            "(GitHub Actions secret of the same name in the release job)"
        )
    return env_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path, help="artifact file(s) to sign or verify")
    parser.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help="private key PEM file (local/operator use; else env NSE_BRIDGE_RELEASE_SIGNING_KEY)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify each artifact against its <name>.sig instead of signing",
    )
    parser.add_argument(
        "--public-key-b64",
        default=None,
        help="base64 raw 32-byte Ed25519 public key (verify mode)",
    )
    parser.add_argument(
        "--public-key-file",
        type=Path,
        default=None,
        help="file holding the base64 public key, e.g. nse_bridge/update_public_key.b64 (verify mode)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="release-manifest.json to check SHA-256 against first (verify mode)",
    )
    parser.add_argument(
        "--show-public-key",
        action="store_true",
        help="print the base64 public key for the signing key and exit (public material)",
    )
    args = parser.parse_args(argv)

    if args.verify and args.show_public_key:
        return _fail("--verify and --show-public-key are mutually exclusive")
    if not args.artifacts and not args.show_public_key:
        return _fail("no artifacts given")

    if args.verify:
        try:
            public_raw = _load_public_key(b64=args.public_key_b64, path=args.public_key_file)
        except ValueError as exc:
            return _fail(str(exc))
        if Ed25519PublicKey is None:
            return _fail("cryptography is not installed — cannot verify")
        return _verify_artifacts(public_raw, args.artifacts, args.manifest)

    try:
        private_key = load_signing_key(_key_source(args.key_file))
    except (ValueError, RuntimeError) as exc:
        return _fail(str(exc))

    if args.show_public_key:
        pub = public_key_b64(private_key)
        print(f"public_key_b64: {pub}")
        print(f"public_key_b64 sha256: {hashlib.sha256(pub.encode()).hexdigest()}")
        return 0
    return _sign_artifacts(private_key, args.artifacts)


if __name__ == "__main__":
    raise SystemExit(main())

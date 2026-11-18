"""Secret store for the NSE Connector Bridge (Phase 1 — security).

Secrets (user API key ``server_token``, ``local_api_key``, device
credential) live ONLY here — never in plaintext ``config.json``, never
in logs, never in exception messages.

Backend selection (in order):
- Windows: ``keyring`` with the Windows Credential Manager backend,
  with a DPAPI file store (``CryptProtectData``) as fallback.
- Non-Windows (Linux dev/CI): if a real ``keyring`` backend is
  available (e.g. Secret Service on a desktop) it is used; otherwise
  a clearly-marked DEV-ONLY file fallback (``dev-secrets.json`` under
  the bridge data dir) is used so unit tests can run. The dev fallback
  REFUSES to run on Windows: a Windows production environment without
  Credential Manager/DPAPI is a hard error, never a silent plaintext
  downgrade.

The dev fallback file carries a ``_warning`` banner and is NOT
``config.json`` — plaintext secrets are migrated OUT of ``config.json``
into the store on first run (see :func:`migrate_plaintext_secrets`).
"""

from __future__ import annotations

import base64
import json
import secrets as _secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BridgeSettings

SERVICE_NAME = "NSEBridge"

#: Secret names managed by this module.
SECRET_KEYS = ("server_token", "local_api_key", "device_credential")

#: Legacy plaintext keys that used to live in config.json. They are
#: dropped (never re-stored) during migration — the MT5 password and
#: the agent LLM key no longer exist anywhere in the bridge.
LEGACY_PLAINTEXT_KEYS = ("mt5_password", "agent_llm_api_key")

DEV_FALLBACK_FILENAME = "dev-secrets.json"
DPAPI_FILENAME = "secrets.dpapi.json"

DEV_FALLBACK_WARNING = (
    "DEV-ONLY secret fallback — NOT for production use. "
    "On Windows, secrets must live in Windows Credential Manager / DPAPI. "
    "Delete this file and re-enter credentials through the secure store."
)


class SecretStoreError(Exception):
    """The secret store is unavailable or refused to operate."""


class SecretStore:
    """Minimal secret-store interface shared by all backends."""

    def get_secret(self, name: str) -> str | None:
        raise NotImplementedError

    def set_secret(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete_secret(self, name: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# keyring (Windows Credential Manager / Secret Service)
# ---------------------------------------------------------------------------
def _keyring_usable() -> bool:
    """True when keyring imports and a real backend answers a probe.

    On this Linux CI VM there is no Secret Service, so keyring reports
    its fail backend and this returns False (the dev fallback is used).
    On Windows the wincred backend answers the probe.
    """
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return False
    try:
        keyring.get_password(f"{SERVICE_NAME}-probe", "probe")
    except keyring.errors.KeyringError:
        return False
    except Exception:  # noqa: BLE001 — any backend failure means unusable
        return False
    return True


class KeyringSecretStore(SecretStore):
    """Secrets in the OS credential store via ``keyring``.

    On Windows this is Windows Credential Manager (DPAPI-backed);
    on Linux desktops it is Secret Service / KWallet when present.
    """

    def __init__(self) -> None:
        try:
            import keyring
        except Exception as exc:  # pragma: no cover — import-time failure
            raise SecretStoreError(f"keyring is not available: {exc}") from exc
        self._keyring = keyring

    def get_secret(self, name: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE_NAME, name)
        except Exception as exc:
            raise SecretStoreError(f"keyring read failed for {name!r}: {exc}") from exc

    def set_secret(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE_NAME, name, value)
        except Exception as exc:
            raise SecretStoreError(f"keyring write failed for {name!r}: {exc}") from exc

    def delete_secret(self, name: str) -> None:
        try:
            self._keyring.delete_password(SERVICE_NAME, name)
        except Exception as exc:
            # Deleting a missing entry is not an error.
            if "not found" in str(exc).lower() or "passwordnotfounderror" in type(exc).__name__.lower():
                return
            raise SecretStoreError(f"keyring delete failed for {name!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# DPAPI file store (Windows fallback when keyring is unavailable)
# ---------------------------------------------------------------------------
class DpapiSecretStore(SecretStore):
    """DPAPI-encrypted secrets in a file under the bridge data dir.

    Windows-only. Values are encrypted with ``CryptProtectData``
    (current-user scope, no UI) and stored base64-encoded, so the file
    is useless without the Windows user profile that created it.
    """

    def __init__(self, data_dir: Path) -> None:
        if sys.platform != "win32":  # pragma: no cover — Windows-only
            raise SecretStoreError("DPAPI secret store is Windows-only")
        try:  # fail closed when DPAPI itself is unavailable
            import ctypes

            ctypes.windll.crypt32  # noqa: B018 — availability probe
        except Exception as exc:
            raise SecretStoreError(f"DPAPI is unavailable: {exc}") from exc
        self._path: Path = Path(data_dir) / DPAPI_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- ctypes plumbing (loaded lazily; windll does not exist on Linux) --
    @staticmethod
    def _crypt32():  # pragma: no cover — Windows-only
        import ctypes

        return ctypes.windll.crypt32

    @staticmethod
    def _protect(data: bytes) -> bytes:  # pragma: no cover — Windows-only
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = DpapiSecretStore._crypt32()
        blob_in = DATA_BLOB(len(data), (ctypes.c_ubyte * len(data)).from_buffer_copy(data))
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0x01,
            ctypes.byref(blob_out),  # CRYPTPROTECT_UI_FORBIDDEN
        ):
            raise SecretStoreError("CryptProtectData failed")
        try:
            return bytes(bytearray(blob_out.pbData[: blob_out.cbData]))
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined] # Windows-only API

    @staticmethod
    def _unprotect(data: bytes) -> bytes:  # pragma: no cover — Windows-only
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = DpapiSecretStore._crypt32()
        blob_in = DATA_BLOB(len(data), (ctypes.c_ubyte * len(data)).from_buffer_copy(data))
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0x01,
            ctypes.byref(blob_out),  # CRYPTPROTECT_UI_FORBIDDEN
        ):
            raise SecretStoreError("CryptUnprotectData failed")
        try:
            return bytes(bytearray(blob_out.pbData[: blob_out.cbData]))
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined] # Windows-only API

    def _read_all(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            return dict(json.loads(self._path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise SecretStoreError(f"cannot read DPAPI secret file: {exc}") from exc

    def _write_all(self, values: dict[str, str]) -> None:
        self._path.write_text(json.dumps(values, indent=2), encoding="utf-8")

    def get_secret(self, name: str) -> str | None:
        raw = self._read_all().get(name)
        if raw is None:
            return None
        return self._unprotect(base64.b64decode(raw.encode("ascii"))).decode("utf-8")

    def set_secret(self, name: str, value: str) -> None:
        values = self._read_all()
        values[name] = base64.b64encode(self._protect(value.encode("utf-8"))).decode("ascii")
        self._write_all(values)

    def delete_secret(self, name: str) -> None:
        values = self._read_all()
        if values.pop(name, None) is not None:
            self._write_all(values)


# ---------------------------------------------------------------------------
# Dev-only file fallback (Linux dev/CI)
# ---------------------------------------------------------------------------
class DevFallbackSecretStore(SecretStore):
    """DEV-ONLY plaintext fallback for Linux dev/CI (unit tests).

    The file is clearly marked (``_warning`` banner) and is NOT
    ``config.json``. It REFUSES to run on Windows — a Windows
    production environment without Credential Manager/DPAPI must fail
    loudly instead of silently downgrading to plaintext.
    """

    def __init__(self, data_dir: Path) -> None:
        if sys.platform == "win32":
            raise SecretStoreError(
                "dev fallback secret store refused on Windows: "
                "no Windows Credential Manager / DPAPI backend is available"
            )
        self._path = Path(data_dir) / DEV_FALLBACK_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SecretStoreError(f"cannot read dev secret file: {exc}") from exc
        secrets = raw.get("secrets") if isinstance(raw, dict) else None
        return dict(secrets) if isinstance(secrets, dict) else {}

    def _write_all(self, values: dict[str, str]) -> None:
        payload = {"_warning": DEV_FALLBACK_WARNING, "secrets": values}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_secret(self, name: str) -> str | None:
        return self._read_all().get(name)

    def set_secret(self, name: str, value: str) -> None:
        values = self._read_all()
        values[name] = value
        self._write_all(values)

    def delete_secret(self, name: str) -> None:
        values = self._read_all()
        if values.pop(name, None) is not None:
            self._write_all(values)


# ---------------------------------------------------------------------------
# Factory + migration + key bootstrap
# ---------------------------------------------------------------------------
def get_secret_store(data_dir: Path, *, allow_dev_fallback: bool = True) -> SecretStore:
    """Return the best available secret store for this platform.

    Windows: keyring (Credential Manager) → DPAPI file store → error.
    Other platforms: keyring when a real backend answers, else the
    dev-only file fallback (or an error when ``allow_dev_fallback``
    is False).
    """
    if sys.platform == "win32":
        if _keyring_usable():
            return KeyringSecretStore()
        try:
            return DpapiSecretStore(data_dir)
        except SecretStoreError:
            raise SecretStoreError(
                "no usable Windows secret store: Windows Credential Manager (keyring) and DPAPI are both unavailable"
            )
    if _keyring_usable():
        return KeyringSecretStore()
    if allow_dev_fallback:
        return DevFallbackSecretStore(data_dir)
    raise SecretStoreError("no keyring backend available and dev fallback is disabled")


def migrate_plaintext_secrets(config_file: Path, store: SecretStore) -> list[str]:
    """Move plaintext secrets out of ``config.json`` into the store.

    Known secrets are stored, then DELETED from the file; legacy keys
    (``mt5_password``, ``agent_llm_api_key``) are dropped without being
    stored — those credentials no longer exist in the bridge. Returns
    the list of secret names migrated (legacy drops are not listed).
    The file is rewritten only when it actually contained a secret or
    legacy key; all other keys are preserved byte-for-byte in meaning.
    """
    path = Path(config_file)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    migrated: list[str] = []
    changed = False
    for key in SECRET_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            store.set_secret(key, value)
            migrated.append(key)
            del raw[key]
            changed = True
    for key in LEGACY_PLAINTEXT_KEYS:
        if key in raw:
            del raw[key]
            changed = True
    if changed:
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    return migrated


def load_secrets_into_settings(settings: BridgeSettings, store: SecretStore) -> list[str]:
    """Fill empty runtime secret fields on ``settings`` from the store.

    Values already present (env vars / explicit config) win and are
    never overwritten. Returns the names that were loaded.
    """
    loaded: list[str] = []
    for key in SECRET_KEYS:
        if not hasattr(settings, key):
            continue  # device_credential has no settings field (yet)
        if getattr(settings, key):
            continue
        value = store.get_secret(key)
        if value:
            setattr(settings, key, value)
            loaded.append(key)
    return loaded


def generate_api_key() -> str:
    """Strong random local API key (256 bits of entropy, URL-safe)."""
    return _secrets.token_urlsafe(32)


def ensure_local_api_key(settings: BridgeSettings, store: SecretStore | None = None) -> str:
    """Guarantee ``settings.local_api_key`` is set — mandatory, never empty.

    Precedence: an already-set key wins; otherwise the store's key;
    otherwise a fresh random key is generated and stored. Raises
    :class:`SecretStoreError` when no store can provide one.
    """
    if settings.local_api_key:
        return settings.local_api_key
    store = store or get_secret_store(settings.data_dir)
    stored = store.get_secret("local_api_key")
    if stored:
        settings.local_api_key = stored
        return stored
    key = generate_api_key()
    store.set_secret("local_api_key", key)
    settings.local_api_key = key
    return key


def initialize_secrets(settings: BridgeSettings, *, strict: bool = False) -> list[str]:
    """First-run secret bootstrap: migrate + load + ensure local API key.

    Returns the migrated secret names. When ``strict`` is False, a
    store failure is swallowed (the caller logs a warning); when
    ``strict`` is True it raises — the bridge must not start with an
    unauthenticated local API.
    """
    try:
        store = get_secret_store(settings.data_dir)
        migrated = migrate_plaintext_secrets(settings.config_file, store)
        load_secrets_into_settings(settings, store)
        ensure_local_api_key(settings, store)
        return migrated
    except SecretStoreError:
        if strict:
            raise
        return []


__all__ = [
    "SECRET_KEYS",
    "SERVICE_NAME",
    "DevFallbackSecretStore",
    "DpapiSecretStore",
    "KeyringSecretStore",
    "SecretStore",
    "SecretStoreError",
    "ensure_local_api_key",
    "generate_api_key",
    "get_secret_store",
    "initialize_secrets",
    "load_secrets_into_settings",
    "migrate_plaintext_secrets",
]

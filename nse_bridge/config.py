"""Bridge configuration: JSON config file + environment overrides.

Precedence: environment (NSE_BRIDGE_*) > config.json > defaults.
The config file lives next to the executable (or ~/.nse-bridge/config.json
in dev), so end users can edit one plain file.

SECRETS (``server_token``, ``local_api_key``, device credential) are
NEVER stored in config.json — they live in the OS secret store
(Windows Credential Manager / DPAPI, see ``nse_bridge/secrets.py``)
and are loaded into these settings at runtime only. ``persist_settings``
writes no secret field. Any plaintext secret found in config.json is
migrated into the store and deleted from the file on first run.

Environment selection: ``environment`` (production | staging | dev,
via ``NSE_BRIDGE_ENVIRONMENT``) + ``server_base_url``. The production
default is ``https://bridge.nexusverse.space``; dev/staging endpoints
come from configuration only — no hardcoded dev endpoint exists here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import BRIDGE_VERSION

PRODUCTION_SERVER_BASE_URL = "https://bridge.nexusverse.space"
PRODUCTION_SERVER_WS_PATH = "/api/v1/connector/ws"
DEV_ENVIRONMENTS = ("dev", "development", "test", "testing", "staging")


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):  # running as PyInstaller exe
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return Path(base) / "NSEBridge"
    return Path.home() / ".nse-bridge"


def is_loopback_host(host: str) -> bool:
    """True for loopback binds only (127.x, ::1, localhost)."""
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


def validate_bind_host(host: str, *, allow_non_loopback: bool) -> None:
    """Refuse a non-loopback bind unless explicitly authorized.

    Raises ValueError with an actionable message; callers turn this
    into a startup refusal (never a silent fallback to loopback).
    """
    if is_loopback_host(host):
        return
    if allow_non_loopback:
        return
    raise ValueError(
        f"refusing non-loopback bind host {host!r}: set allow_non_loopback=true "
        "in config.json (or NSE_BRIDGE_ALLOW_NON_LOOPBACK=1) only if you "
        "deliberately expose the local API, and note the mandatory X-API-Key "
        "auth still applies"
    )


def validate_server_url(url: str, *, allow_insecure: bool = False) -> None:
    """Validate the outbound server WebSocket URL.

    Empty means the server channel is disabled. Otherwise the scheme
    must be ``wss://``; ``ws://`` is accepted only for loopback test
    endpoints or when ``allow_insecure`` is explicitly set (test
    config). ``http(s)://`` is never a valid server-channel URL.
    Raises ValueError on violation.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme == "wss":
        return
    if parsed.scheme == "ws":
        host = (parsed.hostname or "").lower()
        if allow_insecure or host in ("127.0.0.1", "::1", "localhost"):
            return
        raise ValueError(
            f"server_url {url!r} uses ws:// outside a loopback/test endpoint — production server URLs must use wss://"
        )
    raise ValueError(f"server_url {url!r} must use wss:// (TLS is mandatory for server URLs)")


def validate_server_base_url(url: str, *, environment: str = "production") -> None:
    """Validate the server REST base URL (https mandatory in production)."""
    if not url:
        raise ValueError("server_base_url must not be empty")
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and environment.lower() in DEV_ENVIRONMENTS:
        return
    raise ValueError(
        f"server_base_url {url!r} must use https:// (TLS is mandatory; "
        "http:// is only allowed in a dev/test environment)"
    )


class BridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NSE_BRIDGE_", env_file=".env", extra="ignore")

    # --- server -------------------------------------------------------
    app_name: str = "NSE MT5 Bridge"
    version: str = BRIDGE_VERSION

    # --- environment / server base (production default) ---------------
    environment: str = "production"  # production | staging | dev
    server_base_url: str = PRODUCTION_SERVER_BASE_URL

    # --- local API ----------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8788
    # Explicit opt-in for a non-loopback bind; false by default.
    # A non-loopback bind additionally requires the mandatory X-API-Key.
    allow_non_loopback: bool = False
    # Runtime-only secret (from the secret store, never config.json).
    local_api_key: str = ""

    # --- NSE server channel (outbound WSS) ----------------------------
    # Empty = disabled. When set it must be wss:// (see validate_server_url).
    # Default production endpoint (derived from server_base_url):
    #   wss://bridge.nexusverse.space/api/v1/connector/ws
    server_url: str = ""
    # Runtime-only secret (from the secret store, never config.json).
    server_token: str = ""  # auth token issued by the platform
    device_id: str = ""  # auto-generated + persisted when empty
    # CONTRACT v1 §5.4: initial backoff 500 ms, doubling, capped at 30 s.
    reconnect_initial_ms: int = 500
    reconnect_max_ms: int = 30000
    heartbeat_interval_s: float = 15.0

    # --- MetaTrader 5 -------------------------------------------------
    mt5_terminal_path: str = ""  # auto-discovered when empty
    # Comma-separated extra directories searched for terminals
    # (custom + portable installs: a directory containing terminal64.exe
    # counts as a candidate; immediate subdirectories are scanned too).
    # Env: NSE_BRIDGE_MT5_CUSTOM_TERMINAL_DIRS. Discovery only reads —
    # it never overwrites the configured mt5_terminal_path.
    mt5_custom_terminal_dirs: str = ""
    mt5_login: int | None = None  # optional explicit login (fail-safe check only)
    # NOTE: there is deliberately NO mt5_password field. The user signs in
    # through MT5 itself; the bridge never asks for or stores the password.
    mt5_server: str = ""
    mt5_timeout_ms: int = 5000
    mt5_retries: int = 3
    broker_utc_offset_minutes: int = 0  # broker server UTC offset (some brokers deviate)

    # --- updates (CONTRACT v1 §3.5; verified updater, Phase 5) --------
    # Base64 raw Ed25519 public key (32 bytes) that verifies release
    # signatures. Env: NSE_BRIDGE_UPDATE_PUBLIC_KEY. This is PUBLIC
    # key material (safe to store in config.json) — but there is NO
    # default: the real NSE release-signing key has to be provisioned
    # by the operator (env var, config.json, or a bundled
    # nse_bridge/update_public_key.b64 file). With no key configured
    # the updater REFUSES to install anything (signature unverifiable).
    update_public_key: str = ""

    # --- trading identity ----------------------------------------------
    # No hardcoded trading identity. The Bridge stamps NO magic on an
    # order unless one is supplied per command (the ``magic`` request
    # field) or set explicitly as device configuration (PUT /settings
    # or config.json). ``None`` = no magic stamped: MT5 then records
    # the order with magic 0 (indistinguishable from a manual order).
    # Likewise there is no default symbol or timeframe: callers pass
    # them per command, or an operator sets ``bot_symbol`` /
    # ``default_timeframe`` explicitly as device configuration. The
    # server supplies the trading identity per command; the client
    # never invents one.
    magic: int | None = None
    bot_symbol: str = ""
    default_timeframe: str = ""
    strict_magic_guard: bool = False  # True = only touch positions/orders with our magic
    # The guard applies ONLY when a magic IS configured (magic is not
    # None). With no magic configured there is nothing to guard
    # against, so the guard passes (see engine/trading._position_owns).

    # --- streaming ------------------------------------------------------
    stream_account_interval_ms: int = 1000

    # NOTE: the AI-agent fields (LLM base URL / API key / model) and
    # server_rest_base were removed in the client-only refactor; the
    # local AI Agent product itself was deleted in Phase 3. Any such
    # plaintext keys left in an old config.json are still scrubbed by
    # the legacy-key migration in secrets.py.

    # --- logging --------------------------------------------------------
    log_level: str = "INFO"
    # Local diagnostic mode: DEBUG telemetry events are emitted only
    # when this is on (default off, ARCHITECTURE-CONTRACT §8).
    # Env: NSE_BRIDGE_DIAGNOSTIC_MODE=1.
    diagnostic_mode: bool = False
    log_dir: str = ""  # default <data_dir>/logs

    # --- gui ------------------------------------------------------------
    gui_mode: str = "auto"  # auto | webview | browser | none

    data_dir: Path = Field(default_factory=default_data_dir)

    @property
    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir else self.data_dir / "logs"

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def resolved_server_ws_url(self) -> str:
        """Effective server WS URL: explicit server_url, else derived
        from the production-shaped server_base_url."""
        if self.server_url:
            return self.server_url
        base = self.server_base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base + PRODUCTION_SERVER_WS_PATH


def _apply_json_overrides(settings: BridgeSettings) -> BridgeSettings:
    """Merge config.json values over defaults (env vars still win)."""
    path = settings.config_file
    if not path.is_file():
        return settings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    env_prefix = "NSE_BRIDGE_"
    for key, value in raw.items():
        env_key = f"{env_prefix}{key.upper()}"
        if env_key in os.environ:
            continue  # env wins
        if hasattr(settings, key) and value is not None:
            try:
                setattr(settings, key, value)
            except (ValueError, TypeError, AttributeError):
                continue  # invalid config value is skipped, not fatal
    return settings


def load_settings() -> BridgeSettings:
    settings = BridgeSettings()
    settings = _apply_json_overrides(settings)
    # Secrets: migrate any plaintext config.json values into the store,
    # load stored secrets into the runtime-only fields, and ensure the
    # mandatory local API key exists. Best-effort here — main() enforces
    # the key strictly before serving.
    try:
        from .secrets import initialize_secrets

        initialize_secrets(settings, strict=False)
    except Exception:  # noqa: BLE001, S110 — best-effort; main() enforces strictly
        pass
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_log_dir.mkdir(parents=True, exist_ok=True)
    return settings


def persist_settings(settings: BridgeSettings) -> None:
    """Persist editable NON-SECRET fields to config.json.

    Secrets (server_token, local_api_key, device credential) are NEVER
    written here — they live in the secret store.
    """
    payload = {}
    for key in (
        "device_id",
        "server_url",
        "server_base_url",
        "environment",
        "host",
        "port",
        "allow_non_loopback",
        "magic",
        "bot_symbol",
        "mt5_terminal_path",
        "mt5_custom_terminal_dirs",
        "update_public_key",
        "mt5_login",
        "mt5_server",
        "log_level",
        "diagnostic_mode",
        "gui_mode",
        "broker_utc_offset_minutes",
    ):
        payload[key] = getattr(settings, key)
    settings.config_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

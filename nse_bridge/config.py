"""Bridge configuration: JSON config file + environment overrides.

Precedence: environment (NSE_BRIDGE_*) > config.json > defaults.
The config file lives next to the executable (or ~/.nse-bridge/config.json
in dev), so end users can edit one plain file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import BRIDGE_VERSION


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):  # running as PyInstaller exe
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return Path(base) / "NSEBridge"
    return Path.home() / ".nse-bridge"


class BridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NSE_BRIDGE_", env_file=".env", extra="ignore"
    )

    # --- server -------------------------------------------------------
    app_name: str = "NSE MT5 Bridge"
    version: str = BRIDGE_VERSION

    # --- local API ----------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8788
    local_api_key: str = ""  # optional; empty = open (local-only bind)

    # --- NSE server channel (outbound WSS) ----------------------------
    server_url: str = ""  # e.g. wss://bridge.nse-platform.io/ws — empty = disabled
    server_token: str = ""  # auth token issued by the platform
    device_id: str = ""  # auto-generated + persisted when empty
    reconnect_initial_ms: int = 1000
    reconnect_max_ms: int = 30000
    heartbeat_interval_s: float = 15.0

    # --- MetaTrader 5 -------------------------------------------------
    mt5_terminal_path: str = ""  # auto-discovered when empty
    mt5_login: int | None = None  # optional explicit login
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_timeout_ms: int = 5000
    mt5_retries: int = 3
    broker_utc_offset_minutes: int = 0  # broker server UTC offset (some brokers deviate)

    # --- trading identity ----------------------------------------------
    magic: int = 888101
    bot_symbol: str = "XAUUSD"
    default_timeframe: str = "M1"
    strict_magic_guard: bool = False  # True = only touch positions/orders with our magic

    # --- streaming ------------------------------------------------------
    stream_account_interval_ms: int = 1000

    # --- AI Agent ---------------------------------------------------------
    server_rest_base: str = ""  # https://… platform REST base (decision audit)
    agent_llm_base_url: str = ""  # OpenAI-compatible /v1/chat/completions base
    agent_llm_api_key: str = ""  # empty = math-only mode (no LLM review)
    agent_llm_model: str = "gpt-4o-mini"

    # --- logging --------------------------------------------------------
    log_level: str = "INFO"
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


def _apply_json_overrides(settings: BridgeSettings) -> BridgeSettings:
    """Merge config.json values over defaults (env vars still win)."""
    path = settings.config_file
    if not path.is_file():
        return settings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return settings
    env_prefix = "NSE_BRIDGE_"
    for key, value in raw.items():
        env_key = f"{env_prefix}{key.upper()}"
        if env_key in os.environ:
            continue  # env wins
        if hasattr(settings, key) and value is not None:
            try:
                setattr(settings, key, value)
            except Exception:
                pass
    return settings


def load_settings() -> BridgeSettings:
    settings = BridgeSettings()
    settings = _apply_json_overrides(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_log_dir.mkdir(parents=True, exist_ok=True)
    return settings


def persist_settings(settings: BridgeSettings) -> None:
    """Persist editable fields to config.json (device_id, server_url, token...)."""
    payload = {}
    for key in (
        "device_id",
        "server_url",
        "server_token",
        "server_rest_base",
        "host",
        "port",
        "local_api_key",
        "magic",
        "bot_symbol",
        "mt5_terminal_path",
        "mt5_login",
        "mt5_server",
        "log_level",
        "gui_mode",
        "broker_utc_offset_minutes",
        "agent_llm_base_url",
        "agent_llm_api_key",
        "agent_llm_model",
    ):
        payload[key] = getattr(settings, key)
    settings.config_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

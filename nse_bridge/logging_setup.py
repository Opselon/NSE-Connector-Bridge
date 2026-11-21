"""Logging: loguru console + rotating file + JSONL file, stdlib interception.

- console sink: colored, human readable
- bridge.log: rotating 10MB x 10 (operators)
- bridge.jsonl: structured events (machine consumption by the server team)

SECRET REDACTION (Phase 1): every sink runs through a redaction
filter, so tokens/keys/passwords can never leak into logs — even when
they arrive inside an exception message or a formatted value. Register
known secret values with :func:`register_secret` (main() does this
after loading secrets); labelled patterns (Bearer, X-API-Key,
``token=``/``password=``/``api_key=`` ...) are scrubbed regardless.
Use :func:`sanitize_payload` before putting user-controlled data into
error responses / result payloads.
"""

from __future__ import annotations

import inspect
import logging
import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger

_FMT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

REDACTED = "[REDACTED]"

# Labelled secret patterns: the label (incl. separator) is kept, the
# value is replaced. Separators must contain ``=`` or ``:`` so prose
# like "password is required" is NOT redacted.
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(bearer\s+)[^\s\"',}]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), rf"\1{REDACTED}"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|server[_-]?token|"
            r"local[_-]?api[_-]?key|token|password|passwd|secret)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"
        ),
        rf"\1{REDACTED}",
    ),
    # Authorization header with a non-Bearer value (Bearer is handled
    # by the first pattern; the lookahead keeps "Bearer [REDACTED]").
    (
        re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)(?!bearer\b)[^\"'\s,}]+"),
        rf"\1{REDACTED}",
    ),
]

_SECRET_KEYS = ("token", "api_key", "apikey", "password", "passwd", "secret", "authorization")

# Exact secret values registered at runtime (main() after loading the
# secret store). Only values >= 8 chars are kept, so short words can
# never be blanked out of unrelated text.
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a concrete secret value for verbatim redaction."""
    if value and len(value) >= 8:
        _known_secrets.add(value)


def clear_registered_secrets() -> None:
    _known_secrets.clear()


def redact_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub tokens/keys/passwords from a log line or error message."""
    if not text:
        return text
    out = text
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    for value in list(_known_secrets) + [v for v in extra_secrets if v and len(v) >= 8]:
        if value in out:
            out = out.replace(value, REDACTED)
    return out


def sanitize_payload(obj: Any) -> Any:
    """Recursively scrub secrets from a dict/list/str payload.

    Dict keys that name a secret (token/key/password/secret/...) have
    their value replaced wholesale; string leaves are run through
    :func:`redact_text`.
    """
    if isinstance(obj, dict):
        cleaned: dict[Any, Any] = {}
        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(part in key_lower for part in _SECRET_KEYS):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = sanitize_payload(value)
        return cleaned
    if isinstance(obj, (list, tuple)):
        return [sanitize_payload(item) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def make_redaction_filter():
    """Loguru sink filter: redact the record message in place, keep it."""

    def _filter(record: dict[str, Any]) -> bool:
        # redact_text is total for str input; record["message"] is always str.
        record["message"] = redact_text(record["message"])
        return True

    return _filter


class RedactionFilter(logging.Filter):
    """Stdlib logging filter: redact the record message in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError, AttributeError):
            return True  # leave a malformed record alone rather than dropping it
        record.msg = redact_text(message)
        record.args = ()
        return True


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn etc.) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, redact_text(record.getMessage()))


def setup_logging(log_dir: Path, level: str = "INFO", extra_sink=None) -> None:
    redact = make_redaction_filter()
    logger.remove()
    # The console sink is best-effort: a windowed (console=False)
    # PyInstaller exe has sys.stderr = None when no console is attached
    # (double-click launch, default GUI mode) and loguru refuses None
    # sinks with a TypeError that kills the app before its window can
    # open. --console/--diagnostic rebind stderr to a real console
    # first; everywhere else the file + JSONL sinks carry the logs.
    if sys.stderr is not None:
        logger.add(sys.stderr, level=level.upper(), format=_FMT, colorize=True, filter=redact)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        Path(log_dir) / "bridge.log",
        level=level.upper(),
        rotation="10 MB",
        retention=10,
        encoding="utf-8",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        filter=redact,
    )
    logger.add(
        Path(log_dir) / "bridge.jsonl",
        level=level.upper(),
        rotation="20 MB",
        retention=5,
        serialize=True,
        enqueue=True,
        filter=redact,
    )
    if extra_sink is not None:  # in-memory ring for get_bridge_logs
        logger.add(extra_sink, level=level.upper(), enqueue=False, filter=redact)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("uvicorn.access", "httpx", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

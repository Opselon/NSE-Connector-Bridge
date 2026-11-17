"""Logging: loguru console + rotating file + JSONL file, stdlib interception.

- console sink: colored, human readable
- bridge.log: rotating 10MB x 10 (operators)
- bridge.jsonl: structured events (machine consumption by the server team)
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

_FMT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn etc.) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(log_dir: Path, level: str = "INFO", extra_sink=None) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FMT, colorize=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        Path(log_dir) / "bridge.log",
        level=level.upper(),
        rotation="10 MB",
        retention=10,
        encoding="utf-8",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )
    logger.add(
        Path(log_dir) / "bridge.jsonl",
        level=level.upper(),
        rotation="20 MB",
        retention=5,
        serialize=True,
        enqueue=True,
    )
    if extra_sink is not None:  # in-memory ring for get_bridge_logs
        logger.add(extra_sink, level=level.upper(), enqueue=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("uvicorn.access", "httpx", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

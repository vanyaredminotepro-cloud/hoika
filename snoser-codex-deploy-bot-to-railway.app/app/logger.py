"""
Logging configuration for Warlord RP Bot.
"""
from __future__ import annotations
import logging
import logging.handlers
import sys
from pathlib import Path

from config import LOG_DIR, BASE_DIR

def setup_logging(level: str = "INFO") -> None:
    """Configure logging with file rotation and console output."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format, date_format))
    root.addHandler(console)

    # File handler with rotation (daily, keep 30 days)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "warlord.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    root.addHandler(file_handler)

    # Separate error file
    error_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "warlord_errors.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(log_format, date_format))
    root.addHandler(error_handler)

    # Security log
    sec_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "security.log",
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8"
    )
    sec_handler.setFormatter(logging.Formatter(log_format, date_format))
    sec_logger = logging.getLogger("security")
    sec_logger.addHandler(sec_handler)
    sec_logger.setLevel(logging.INFO)
    sec_logger.propagate = False

    # Reduce noise from libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.info("Logging initialized (level=%s)", level)
"""Structured logging helpers shared by every Revora module.

Every decision the system makes has to be reconstructable afterwards for the
audit trail, so the convention is to log a JSON payload rather than an
interpolated string.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("revora")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"revora.{name}")


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Log a message with a JSON payload appended, for machine-readable traces."""
    logger.info("%s %s", message, json.dumps(fields, default=str, sort_keys=True))

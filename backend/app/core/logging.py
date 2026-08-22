"""Structured logging helpers shared by every RecoverX module.

Everything the system decides has to be reconstructable later for the X-Ray
audit trail (GROUND_TRUTH.md Day 8-10), so the convention here is: log a dict,
not an f-string.
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
    root = logging.getLogger("recoverx")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"recoverx.{name}")


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """Log a message with a JSON payload appended, for machine-readable traces."""
    logger.info("%s %s", message, json.dumps(fields, default=str, sort_keys=True))

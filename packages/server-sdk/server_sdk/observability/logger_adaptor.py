"""Minimal logger adaptor.

A single ``get_logger`` entry point backed by stdlib logging — no OpenTelemetry
and no structured-log machinery on the serving path.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured stdlib logger."""
    _configure()
    return logging.getLogger(name)

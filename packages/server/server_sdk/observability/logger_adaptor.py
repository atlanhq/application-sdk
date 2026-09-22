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
    # ATLAN_LOG_LEVEL is the primary name -- it is what the fleet sets, and
    # application_sdk reads it first -- with LOG_LEVEL as the fallback. Reading
    # only LOG_LEVEL meant a tenant raising the level got no effect at all.
    requested = (
        os.getenv("ATLAN_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    ).upper()
    # basicConfig raises on an unknown level, and this runs at import of any
    # server_sdk module -- so one typo in a chart value would crashloop the whole
    # host, every app with it, rather than degrading one log line.
    unusable = requested not in logging.getLevelNamesMapping()
    level = "INFO" if unusable else requested
    # Exactly one basicConfig call: it configures the root logger only the first
    # time, so a warning emitted before it would fix the level at WARNING.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if unusable:
        logging.getLogger(__name__).warning(
            "Ignoring unusable log level %r; falling back to INFO.", requested
        )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured stdlib logger."""
    _configure()
    return logging.getLogger(name)

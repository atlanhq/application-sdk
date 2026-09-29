"""Logger lookup for code that ships in the api distribution.

The handler surface (errors, handler, contracts, credential specs) is published
twice: inside ``atlan-application-sdk`` and on its own as
``atlan-application-sdk-api``, which the consolidated API host installs without
the worker SDK. Those files log through this helper. With the full SDK installed
it returns the SDK's structured logger, exactly as before; with only the api
distribution installed ``application_sdk.observability`` does not exist, and it
falls back to the standard library.
"""

from __future__ import annotations

import logging
from typing import Any

_WORKER_MODULES = frozenset(
    {"application_sdk.observability", "application_sdk.observability.logger_adaptor"}
)


def get_logger(name: str) -> Any:
    """The SDK logger for ``name``, or a stdlib logger on an api-only install."""
    try:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — absent on an api-only install
            get_logger as _sdk_get_logger,
        )
    except ModuleNotFoundError as exc:
        if exc.name not in _WORKER_MODULES:
            raise
        return logging.getLogger(name)
    return _sdk_get_logger(name)

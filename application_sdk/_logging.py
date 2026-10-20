"""Logger lookup for code that ships in the api distribution.

The handler surface (errors, handler, contracts, credential specs) ships in
``atlan-application-sdk-api``, which ``atlan-application-sdk`` depends on and
which the consolidated API host installs without the worker SDK. Those files log
through this helper. With the full SDK installed it returns the SDK's structured
logger (``observability.logger_adaptor.get_logger``), exactly as before; with
only the api distribution installed ``application_sdk.observability`` does not
exist, and it falls back to the standard library.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from application_sdk._install import worker_only_missing

_ExcInfo = bool | BaseException | None


class SdkLogger(Protocol):
    """The logging calls both loggers :func:`get_logger` returns accept.

    Satisfied by the SDK's ``AtlanLoggerAdapter`` (full install) and by
    :class:`logging.Logger` (api-only install), so code here may use only what
    both take: ``%``-style arguments and the stdlib keywords.
    """

    def debug(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def info(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def warning(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def error(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def exception(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def critical(
        self,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...

    def log(
        self,
        level: int,
        msg: str,
        /,
        *args: object,
        exc_info: _ExcInfo = ...,
        stack_info: bool = ...,
        stacklevel: int = ...,
        extra: Mapping[str, object] | None = ...,
    ) -> None: ...


def get_logger(name: str | None = None) -> SdkLogger:
    """The SDK logger for ``name``, or a stdlib logger on an api-only install.

    Same signature as :func:`application_sdk.observability.logger_adaptor.get_logger`,
    which the modules using this helper re-exported before.
    """
    try:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — absent on an api-only install
            get_logger as _sdk_get_logger,
        )
    except ModuleNotFoundError as exc:
        if not worker_only_missing(exc):
            raise
        return logging.getLogger(name or __name__)
    return _sdk_get_logger(name)

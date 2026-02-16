"""Light logger for APPLICATION_MODE=SERVER to avoid loading pandas/dapr/observability."""

from typing import Any, Optional

from loguru import logger as _loguru_logger

_light_loggers: dict = {}


class _LightLogger:
    """Minimal logger that forwards to loguru. Same .info/.error/.warning/.debug/.exception API."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._log = _loguru_logger.bind(logger_name=name)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.info(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.error(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.warning(msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.debug(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.exception(msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.critical(msg, *args, **kwargs)


def get_logger(name: Optional[str] = None) -> _LightLogger:
    if name is None:
        name = "application_sdk.observability.logger_adaptor_light"
    if name not in _light_loggers:
        _light_loggers[name] = _LightLogger(name)
    return _light_loggers[name]


default_logger = get_logger()

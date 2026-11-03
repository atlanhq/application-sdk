"""``get_logger`` for app code that the consolidated API host also imports.

An app file the hosted handler imports (a shared client, say) also runs on the
worker. Its usual import, ``application_sdk.observability.logger_adaptor``,
does not exist on the host. This path is the same function there: the SDK's
structured logger with the full SDK installed, stdlib logging without it.
"""

from application_sdk._logging import get_logger

__all__ = ["get_logger"]

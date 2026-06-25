"""Dynamic app and handler loading from module paths.

Provides utilities for loading App and Handler classes from module path
strings, enabling dynamic discovery at container startup.

Module paths use the format: "module.path:ClassName"
For example: "my_package.apps:MyApp"

Usage::

    from application_sdk.discovery import load_app_class, load_handler_class

    app_class = load_app_class("my_package.apps:MyApp")
    handler_class = load_handler_class("my_package.apps:MyApp")
"""

from __future__ import annotations

import importlib
import inspect
import warnings
from typing import Any, ClassVar

from application_sdk._discovery_errors import (
    DiscoveryAppRegistrationError,
    DiscoveryClassNotFoundError,
    DiscoveryHandlerInvalidError,
    DiscoveryModuleImportError,
    DiscoveryPathFormatError,
    DiscoveryTypeMismatchError,
)
from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry
from application_sdk.errors import DISCOVERY_ERROR, ErrorCode
from application_sdk.errors.leaves import InvalidInputError
from application_sdk.handler.base import Handler
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class DiscoveryError(InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY"

    def __init__(
        self,
        message: str,
        *,
        module_path: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "DiscoveryError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        InvalidInputError.__init__(self, message=message, cause=cause)
        self.module_path = module_path
        self._legacy_error_code = error_code or DISCOVERY_ERROR

    @property
    def error_code(self) -> ErrorCode:
        return self._legacy_error_code

    def __str__(self) -> str:
        parts = [f"[{self._legacy_error_code}] {self.message}"]
        if self.module_path:
            parts.append(f"module_path={self.module_path}")
        if self.cause:
            parts.append(f"cause={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


def _parse_module_path(module_path: str) -> tuple[str, str]:
    """Parse "module.name:ClassName" into (module_name, class_name).

    Raises:
        DiscoveryPathFormatError: If the format is invalid.
    """
    if ":" not in module_path:
        raise DiscoveryPathFormatError(
            message=f"Invalid module path format: expected 'module.name:ClassName', got '{module_path}'",
            module_path=module_path,
        )

    module_name, class_name = module_path.split(":", 1)
    if not module_name:
        raise DiscoveryPathFormatError(
            message=f"Empty module name in module path: '{module_path}'",
            module_path=module_path,
        )
    if not class_name:
        raise DiscoveryPathFormatError(
            message=f"Empty class name in module path: '{module_path}'",
            module_path=module_path,
        )

    return module_name, class_name


def _import_class(module_name: str, class_name: str) -> type[Any]:
    """Import a class from a module by name.

    Raises:
        DiscoveryModuleImportError: If the module cannot be imported.
        DiscoveryClassNotFoundError: If the class is not in the module.
        DiscoveryTypeMismatchError: If the symbol is not a class.
    """
    module_path = f"{module_name}:{class_name}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:  # conformance: ignore[E008] re-raising as typed DiscoveryModuleImportError; caller logs
        raise DiscoveryModuleImportError(
            message=f"Cannot import module '{module_name}'",
            module_path=module_path,
            cause=e,
        ) from e
    # conformance: ignore[E004] re-raising as typed DiscoveryModuleImportError; caller logs
    except Exception as e:
        raise DiscoveryModuleImportError(
            message=f"Error importing module '{module_name}'",
            module_path=module_path,
            cause=e,
        ) from e

    if not hasattr(module, class_name):
        available = [name for name, obj in inspect.getmembers(module, inspect.isclass)]
        raise DiscoveryClassNotFoundError(
            message=(
                f"Class '{class_name}' not found in module '{module_name}'. "
                f"Available classes: {available}"
            ),
            module_path=module_path,
        )

    cls = getattr(module, class_name)
    if not isinstance(cls, type):
        raise DiscoveryTypeMismatchError(
            message=f"'{class_name}' in module '{module_name}' is not a class",
            module_path=module_path,
        )

    return cls


def _is_app_class(cls: type[Any]) -> bool:
    """Return True if cls is an App subclass (not App itself)."""
    return isinstance(cls, type) and issubclass(cls, App) and cls is not App


def _is_handler_class(cls: type[Any]) -> bool:
    """Return True if cls is a Handler subclass (not Handler itself)."""
    return isinstance(cls, type) and issubclass(cls, Handler) and cls is not Handler


def load_app_class(module_path: str) -> type[App]:
    """Load an App class from a module path.

    Importing the module triggers ``App.__init_subclass__``, which registers
    the class in AppRegistry and wraps it as a Temporal workflow.

    Args:
        module_path: Module path in "module.name:ClassName" format.

    Returns:
        The App subclass.

    Raises:
        DiscoveryTypeMismatchError: If the class isn't an App subclass.

    Example::

        app_class = load_app_class("my_package.apps:MyApp")
    """
    module_name, class_name = _parse_module_path(module_path)
    cls = _import_class(module_name, class_name)

    if not _is_app_class(cls):
        raise DiscoveryTypeMismatchError(
            message=(
                f"Class '{class_name}' is not an App subclass. "
                "Ensure it inherits from App."
            ),
            module_path=module_path,
        )

    logger.info("Loaded app class: %s from %s", class_name, module_path)
    return cls  # type: ignore[return-value]


def load_handler_class(
    module_path: str,
    *,
    handler_module_path: str | None = None,
) -> type[Handler] | None:
    """Load a Handler class associated with an App.

    Handler discovery order:
    1. If ``handler_module_path`` is provided, load from that path.
    2. Look for ``{AppClassName}Handler`` in the same module.
    3. Look for any Handler subclass in the same module.
    4. Return None if no handler is found.

    Args:
        module_path: Module path of the App class.
        handler_module_path: Optional explicit path to a Handler class.

    Returns:
        The Handler subclass, or None if not found.

    Raises:
        DiscoveryHandlerInvalidError: If ``handler_module_path`` is provided but invalid.
    """
    if handler_module_path:
        module_name, class_name = _parse_module_path(handler_module_path)
        cls = _import_class(module_name, class_name)

        if not _is_handler_class(cls):
            raise DiscoveryHandlerInvalidError(
                message=f"Class '{class_name}' is not a Handler subclass.",
                module_path=handler_module_path,
            )

        logger.info(
            "Loaded handler class from explicit path handler_module_path=%s class=%s",
            handler_module_path,
            class_name,
        )
        return cls  # type: ignore[return-value]

    # Convention-based discovery
    module_name, app_class_name = _parse_module_path(module_path)
    handler_class_name = f"{app_class_name}Handler"

    try:
        module = importlib.import_module(module_name)
    except ImportError:  # conformance: ignore[E008] convention-based handler discovery; None = module not found
        return None  # conformance: ignore[E007] convention-based discovery probe; None return means module absent, not an error

    if hasattr(module, handler_class_name):
        cls = getattr(module, handler_class_name)
        if _is_handler_class(cls):
            logger.info(
                "Loaded handler class by convention module_path=%s class=%s",
                module_path,
                handler_class_name,
            )
            return cls  # type: ignore[return-value]

    # Fall back to scanning for any Handler subclass
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Handler) and obj is not Handler:
            logger.info(
                "Found handler class by type inspection module_path=%s class=%s",
                module_path,
                name,
            )
            return obj  # type: ignore[return-value]

    logger.debug(
        "No handler class found for app module_path=%s tried_class=%s",
        module_path,
        handler_class_name,
    )
    return None


def validate_app_class(cls: type[App]) -> None:
    """Validate that an App class is properly registered.

    Checks that the class has ``_app_name`` and ``_app_version`` attributes
    (set by ``App.__init_subclass__``) and is registered in AppRegistry.

    Raises:
        DiscoveryAppRegistrationError: If validation fails.
    """
    if not _is_app_class(cls):
        raise DiscoveryAppRegistrationError(
            message=f"Class {cls.__name__} is not an App subclass"
        )

    if not hasattr(cls, "_app_name"):
        raise DiscoveryAppRegistrationError(
            message=(
                f"Class {cls.__name__} is missing _app_name. "
                "Ensure it inherits from App correctly."
            )
        )

    if not hasattr(cls, "_app_version"):
        raise DiscoveryAppRegistrationError(
            message=(
                f"Class {cls.__name__} is missing _app_version. "
                "Ensure it inherits from App correctly."
            )
        )

    registry = AppRegistry.get_instance()
    app_name = cls._app_name  # type: ignore[attr-defined]

    if app_name not in registry.list_apps():
        raise DiscoveryAppRegistrationError(
            message=(
                f"App '{app_name}' is not registered in AppRegistry. "
                "This usually means App.__init_subclass__ did not run correctly."
            )
        )

    logger.debug(
        "App class validated app_name=%s version=%s",
        app_name,
        cls._app_version,  # type: ignore[attr-defined]
    )

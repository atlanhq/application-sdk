"""Typed error leaves for the discovery module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class DiscoveryPathFormatError(InvalidInputError):
    """Module path string is not in 'module.name:ClassName' format."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_PATH_FORMAT"
    module_path: str = ""


@dataclass(kw_only=True)
class DiscoveryModuleImportError(InvalidInputError):
    """Module could not be imported."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_MODULE_IMPORT"
    module_path: str = ""


@dataclass(kw_only=True)
class DiscoveryClassNotFoundError(InvalidInputError):
    """Named class does not exist in the imported module."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_CLASS_NOT_FOUND"
    module_path: str = ""


@dataclass(kw_only=True)
class DiscoveryTypeMismatchError(InvalidInputError):
    """Loaded symbol is not the expected type (App or Handler subclass)."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_TYPE_MISMATCH"
    module_path: str = ""


@dataclass(kw_only=True)
class DiscoveryHandlerInvalidError(InvalidInputError):
    """Explicitly specified handler path does not point to a Handler subclass."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_HANDLER_INVALID"
    module_path: str = ""


@dataclass(kw_only=True)
class DiscoveryAppRegistrationError(InvalidInputError):
    """App class failed post-load registration validation."""

    code: ClassVar[str] = "INVALID_INPUT_DISCOVERY_APP_REGISTRATION"

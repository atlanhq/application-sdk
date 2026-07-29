"""Typed error leaves for the query transformer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DataIntegrityError,
    InternalError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class BuildStructLevelRequiredError(InternalError):
    code: ClassVar[str] = "INTERNAL_QUERY_BUILD_STRUCT_LEVEL"
    message: str = "level cannot be None in _build_struct"
    invariant: str | None = "level_required"


@dataclass(kw_only=True)
class BuildStructPrefixRequiredError(InternalError):
    code: ClassVar[str] = "INTERNAL_QUERY_BUILD_STRUCT_PREFIX"
    message: str = "prefix cannot be None in _build_struct"
    invariant: str | None = "prefix_required"


@dataclass(kw_only=True)
class IncompatibleDefaultTypeError(DataIntegrityError):
    """An app-config collision: a template default cannot be represented in
    the type of the extractor column sharing its name. Fixed by the app owner
    renaming the colliding key — not an SDK invariant violation."""

    code: ClassVar[str] = "DATA_INTEGRITY_QUERY_DEFAULT_TYPE_MISMATCH"
    message: str = (
        "Default value type is incompatible with the colliding source column type"
    )


@dataclass(kw_only=True)
class SqlTransformNotRegisteredError(UnimplementedError):
    code: ClassVar[str] = "UNIMPLEMENTED_QUERY_SQL_TRANSFORM"
    message: str = "No SQL transformation registered"
    operation: str | None = "sql_transform"

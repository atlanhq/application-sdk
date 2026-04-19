"""Built-in App templates for common extraction patterns.

Provides v3-style App subclasses intended to be subclassed by connectors for SQL
metadata extraction and query extraction. These replace the v2 workflow/activities
split with typed, single-class implementations.

Usage::

    from application_sdk.templates import SqlMetadataExtractor, SqlQueryExtractor
    from application_sdk.templates import IncrementalSqlMetadataExtractor
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
    from application_sdk.templates.incremental_sql_metadata_extractor import (
        IncrementalSqlMetadataExtractor,
    )
    from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor
    from application_sdk.templates.sql_query_extractor import SqlQueryExtractor

__all__ = [
    "BaseMetadataExtractor",
    "IncrementalSqlMetadataExtractor",
    "SqlMetadataExtractor",
    "SqlQueryExtractor",
]

_module_map = {
    "BaseMetadataExtractor": "application_sdk.templates.base_metadata_extractor",
    "IncrementalSqlMetadataExtractor": "application_sdk.templates.incremental_sql_metadata_extractor",
    "SqlMetadataExtractor": "application_sdk.templates.sql_metadata_extractor",
    "SqlQueryExtractor": "application_sdk.templates.sql_query_extractor",
}


def __getattr__(name: str) -> object:
    if name in _module_map:
        import importlib

        module = importlib.import_module(_module_map[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

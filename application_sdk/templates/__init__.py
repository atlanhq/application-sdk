"""Built-in App templates for common extraction patterns.

Provides v3-style App subclasses intended to be subclassed by connectors for SQL
metadata extraction and query extraction. These replace the v2 workflow/activities
split with typed, single-class implementations.

Usage::

    from application_sdk.templates import SqlMetadataExtractor, SqlQueryExtractor
    from application_sdk.templates import IncrementalSqlMetadataExtractor
"""

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

"""Metadata extraction activities for SQL databases.

This module provides:
- BaseSQLMetadataExtractionActivities: Base class for SQL metadata extraction
- IncrementalMetadataExtractionMixin: Mixin for incremental extraction (Approach A)
"""

from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)
from application_sdk.activities.metadata_extraction.incremental import (
    IncrementalMetadataExtractionMixin,
)

__all__ = [
    "BaseSQLMetadataExtractionActivities",
    "IncrementalMetadataExtractionMixin",
]

"""Common constants used across the documentation parsers.

This module centralizes constants used by various parser modules, including
manifest file names and other shared configuration values.
"""

from typing import Tuple

"""Valid file names for customer documentation manifest files.

These files define the structure and metadata for customer-facing documentation.
Supports both .yaml and .yml extensions.
"""
CUSTOMER_MANIFEST_FILE_NAMES: Tuple[str, ...] = (
    "docs.customer.manifest.yaml",
    "docs.customer.manifest.yml",
)

"""Valid file names for internal documentation manifest files.

These files define the structure and metadata for internal documentation.
Supports both .yaml and .yml extensions.
"""
INTERNAL_MANIFEST_FILE_NAMES: Tuple[str, ...] = (
    "docs.internal.manifest.yaml",
    "docs.internal.manifest.yml",
)

"""Common activities utilities and models.

This module provides shared utilities and models for SDK activities,
including SQL validation, structured configuration models, and helper functions.
"""

from application_sdk.activities.common.sql_models import (
    Identifier,
    MinerConfig,
    RangeExpression,
    SQLDialect,
)
from application_sdk.activities.common.sql_validation import (
    SAFE_IDENTIFIER_PATTERN,
    SAFE_PLACEHOLDER_PATTERN,
    UNSAFE_SQL_FRAGMENT_PATTERN,
    UNSAFE_TEMPLATE_PATTERN,
    require_identifier_if_placeholder,
    validate_custom_template,
    validate_identifier,
    validate_marker_value,
    validate_numeric_value,
    validate_placeholder,
    validate_range_placeholders,
    validate_sql_fragment,
)

__all__ = [
    # SQL Models
    "Identifier",
    "MinerConfig",
    "RangeExpression",
    "SQLDialect",
    # Validation patterns
    "SAFE_IDENTIFIER_PATTERN",
    "SAFE_PLACEHOLDER_PATTERN",
    "UNSAFE_SQL_FRAGMENT_PATTERN",
    "UNSAFE_TEMPLATE_PATTERN",
    # Validation functions
    "require_identifier_if_placeholder",
    "validate_custom_template",
    "validate_identifier",
    "validate_marker_value",
    "validate_numeric_value",
    "validate_placeholder",
    "validate_range_placeholders",
    "validate_sql_fragment",
]

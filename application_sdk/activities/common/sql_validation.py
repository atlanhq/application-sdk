from __future__ import annotations

import re
from typing import Optional

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SAFE_PLACEHOLDER_PATTERN = re.compile(r"^[\[\]{}A-Za-z0-9_]+$")
UNSAFE_SQL_FRAGMENT_PATTERN = re.compile(r"(;|--|/\*|\*/)")


def validate_identifier(
    value: Optional[str], field_name: str, *, allow_empty: bool = False
) -> Optional[str]:
    """Validate SQL identifiers only contain safe characters."""
    if value is None or value == "":
        if allow_empty:
            return value
        raise ValueError(f"{field_name} is required")
    if not SAFE_IDENTIFIER_PATTERN.match(value):
        raise ValueError(f"Invalid {field_name} value: {value}")
    return value


def validate_sql_fragment(value: str, field_name: str) -> str:
    """Reject SQL fragments containing unsafe tokens."""
    if not value or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    if UNSAFE_SQL_FRAGMENT_PATTERN.search(value):
        raise ValueError(f"{field_name} contains unsafe SQL tokens")
    return value


def validate_placeholder(value: str, field_name: str) -> str:
    """Validate placeholder tokens used for ranged SQL replacements."""
    if not SAFE_PLACEHOLDER_PATTERN.match(value):
        raise ValueError(f"Invalid {field_name} value: {value}")
    return value


def validate_marker_value(value: object, field_name: str) -> str:
    """Validate that marker values are numeric strings."""
    if value is None:
        raise ValueError(f"{field_name} is required")
    marker_value = str(value)
    if not marker_value.isdigit():
        raise ValueError(f"{field_name} must be a numeric value")
    return marker_value


def require_identifier_if_placeholder(
    template: str, placeholder: str, value: Optional[str]
) -> None:
    """Validate identifier only if its placeholder appears in the template."""
    if f"{{{placeholder}}}" not in template:
        return
    validate_identifier(value, placeholder, allow_empty=False)


def validate_range_placeholders(
    template: str,
    sql_replace_from: str,
    sql_replace_to: str,
    ranged_sql_start_key: str,
    ranged_sql_end_key: str,
) -> None:
    """Ensure sql_replace_to includes both range placeholders when used."""
    uses_replace = "{sql_replace_from}" in template or (
        sql_replace_from and sql_replace_from in template
    )
    if uses_replace:
        if ranged_sql_start_key not in sql_replace_to:
            raise ValueError("sql_replace_to must include ranged_sql_start_key")
        if ranged_sql_end_key not in sql_replace_to:
            raise ValueError("sql_replace_to must include ranged_sql_end_key")

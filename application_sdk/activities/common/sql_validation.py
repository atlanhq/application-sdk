from __future__ import annotations

import re
from typing import Any, Optional

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SAFE_PLACEHOLDER_PATTERN = re.compile(r"^[\[\]{}A-Za-z0-9_]+$")
UNSAFE_SQL_FRAGMENT_PATTERN = re.compile(r"(;|--|/\*|\*/)")

# Extended pattern for custom templates - blocks DDL/DML keywords
UNSAFE_TEMPLATE_PATTERN = re.compile(
    r"(;|--|/\*|\*/|\bUNION\b|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b"
    r"|\bDROP\b|\bCREATE\b|\bALTER\b|\bGRANT\b|\bTRUNCATE\b|\bEXEC\b)",
    re.IGNORECASE,
)


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


def validate_numeric_value(value: Any, field_name: str) -> int:
    """Validate that a value is a non-negative integer.

    Args:
        value: Value to validate.
        field_name: Name for error messages.

    Returns:
        Validated integer value.

    Raises:
        ValueError: If value is not a non-negative integer.
    """
    if value is None:
        raise ValueError(f"{field_name} is required")
    try:
        result = int(str(value).strip())
        if result < 0:
            raise ValueError(f"{field_name} must be non-negative, got {result}")
        return result
    except (ValueError, TypeError) as e:
        raise ValueError(f"{field_name} must be a numeric value, got: {value}") from e


def validate_custom_template(
    template: str,
    start_placeholder: str,
    end_placeholder: str,
    field_name: str = "custom_template",
) -> str:
    """Validate a custom SQL template for safety.

    Args:
        template: The SQL template to validate.
        start_placeholder: Required start placeholder.
        end_placeholder: Required end placeholder.
        field_name: Name for error messages.

    Returns:
        Validated template string.

    Raises:
        ValueError: If template is invalid or unsafe.
    """
    if not template or not template.strip():
        raise ValueError(f"{field_name} cannot be empty")

    # Check for unsafe SQL tokens
    if UNSAFE_TEMPLATE_PATTERN.search(template):
        raise ValueError(
            f"{field_name} contains unsafe SQL tokens. "
            "Templates must not contain: ;, --, /*, */, "
            "UNION, SELECT, INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, GRANT, "
            "TRUNCATE, EXEC"
        )

    # Check for required placeholders
    if start_placeholder not in template:
        raise ValueError(
            f"{field_name} must contain start placeholder '{start_placeholder}'"
        )
    if end_placeholder not in template:
        raise ValueError(
            f"{field_name} must contain end placeholder '{end_placeholder}'"
        )

    return template

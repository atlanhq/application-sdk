"""Structured SQL models for secure query extraction.

This module provides validated SQL configuration models that replace the
vulnerable string-based MinerArgs approach. These models enforce strict
validation to prevent SQL injection attacks.

Classes:
    SQLDialect: Supported SQL dialects for identifier quoting.
    Identifier: Validated SQL identifier with dialect-specific rendering.
    RangeExpression: Structured range filtering with custom template support.
    MinerConfig: Complete structured configuration for SQL query mining.
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, ClassVar, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from application_sdk.activities.common.sql_validation import (
    SAFE_IDENTIFIER_PATTERN,
    SAFE_PLACEHOLDER_PATTERN,
)


class SQLDialect(str, Enum):
    """Supported SQL dialects for identifier quoting.

    Each dialect has specific quoting rules for identifiers:
    - SNOWFLAKE, REDSHIFT, POSTGRES: Use double quotes ("identifier")
    - BIGQUERY, MYSQL: Use backticks (`identifier`)
    - GENERIC: No quoting (returns validated identifier as-is)
    """

    GENERIC = "generic"
    SNOWFLAKE = "snowflake"
    REDSHIFT = "redshift"
    POSTGRES = "postgres"
    BIGQUERY = "bigquery"
    MYSQL = "mysql"


class Identifier(BaseModel):
    """Validated SQL identifier with dialect-specific rendering.

    This class ensures SQL identifiers only contain safe characters and
    provides proper quoting for different database dialects.

    Attributes:
        value: The identifier value (e.g., "ACCOUNT_USAGE", "query_history")
    """

    value: str = Field(..., description="The SQL identifier value")

    model_config = ConfigDict(frozen=True)

    @field_validator("value")
    @classmethod
    def validate_identifier_value(cls, v: str) -> str:
        """Validate identifier against safe pattern."""
        if not v or not v.strip():
            raise ValueError("Identifier cannot be empty")
        if not SAFE_IDENTIFIER_PATTERN.match(v):
            raise ValueError(
                f"Invalid identifier '{v}': must start with letter or underscore, "
                "contain only alphanumeric characters, underscores, or dollar signs"
            )
        return v

    def render(self, dialect: SQLDialect = SQLDialect.GENERIC) -> str:
        """Render the identifier with dialect-specific quoting.

        Args:
            dialect: The SQL dialect for quoting rules.

        Returns:
            Quoted identifier string safe for SQL.
        """
        if dialect in (SQLDialect.MYSQL, SQLDialect.BIGQUERY):
            return f"`{self.value}`"
        elif dialect in (
            SQLDialect.SNOWFLAKE,
            SQLDialect.POSTGRES,
            SQLDialect.REDSHIFT,
        ):
            return f'"{self.value}"'
        else:
            # Generic: return as-is (already validated)
            return self.value

    def __str__(self) -> str:
        """Return unquoted value for string representation."""
        return self.value


# Pattern to detect unsafe SQL tokens in custom templates
UNSAFE_TEMPLATE_PATTERN: re.Pattern[str] = re.compile(
    r"(;|--|/\*|\*/|\bUNION\b|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b"
    r"|\bDROP\b|\bCREATE\b|\bALTER\b|\bGRANT\b|\bTRUNCATE\b|\bEXEC\b)",
    re.IGNORECASE,
)


class RangeExpression(BaseModel):
    """Structured range filtering expression with custom template support.

    This class replaces the vulnerable sql_replace_from/sql_replace_to pattern
    with a structured approach that validates custom templates.

    Attributes:
        timestamp_column: The column used for time-based filtering.
        start_placeholder: Placeholder string for range start.
        end_placeholder: Placeholder string for range end.
        custom_template: Optional custom SQL template for the range expression.
    """

    timestamp_column: Identifier = Field(
        ..., description="Column for time-based filtering"
    )
    start_placeholder: str = Field(
        default="[START_MARKER]", description="Placeholder for range start value"
    )
    end_placeholder: str = Field(
        default="[END_MARKER]", description="Placeholder for range end value"
    )
    custom_template: Optional[str] = Field(
        default=None, description="Custom SQL template for range expression"
    )

    model_config = ConfigDict(frozen=True)

    @field_validator("start_placeholder", "end_placeholder")
    @classmethod
    def validate_placeholder(cls, v: str) -> str:
        """Validate placeholder tokens."""
        if not v or not v.strip():
            raise ValueError("Placeholder cannot be empty")
        if not SAFE_PLACEHOLDER_PATTERN.match(v):
            raise ValueError(
                f"Invalid placeholder '{v}': must match pattern [\\[\\]{{}}A-Za-z0-9_]+"
            )
        return v

    @field_validator("custom_template")
    @classmethod
    def validate_custom_template_safety(cls, v: Optional[str]) -> Optional[str]:
        """Validate custom template for unsafe SQL patterns."""
        if v is None:
            return v
        if not v.strip():
            raise ValueError("custom_template cannot be empty if provided")
        if UNSAFE_TEMPLATE_PATTERN.search(v):
            raise ValueError(
                "custom_template contains unsafe SQL tokens. "
                "Templates must not contain: ;, --, /*, */, "
                "UNION, SELECT, INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, GRANT, "
                "TRUNCATE, EXEC"
            )
        return v

    @model_validator(mode="after")
    def validate_template_placeholders(self) -> RangeExpression:
        """Ensure custom template contains required placeholders."""
        if self.custom_template is not None:
            if self.start_placeholder not in self.custom_template:
                raise ValueError(
                    f"custom_template must contain start_placeholder "
                    f"'{self.start_placeholder}'"
                )
            if self.end_placeholder not in self.custom_template:
                raise ValueError(
                    f"custom_template must contain end_placeholder "
                    f"'{self.end_placeholder}'"
                )
        return self

    def render(
        self, dialect: SQLDialect, start: int, end: int
    ) -> Tuple[str, Dict[str, Any]]:
        """Render the range expression with values.

        Args:
            dialect: SQL dialect for identifier quoting.
            start: Start timestamp value (must be non-negative integer).
            end: End timestamp value (must be non-negative integer).

        Returns:
            Tuple of (SQL fragment string, empty dict for compatibility).

        Raises:
            ValueError: If start or end is not a non-negative integer.

        Note:
            Values are validated as numeric and directly substituted.
            The SQL client doesn't support parameterized queries, so we
            rely on strict numeric validation instead.
        """
        # Validate numeric values
        if not isinstance(start, int) or start < 0:
            raise ValueError(f"start must be a non-negative integer, got {start}")
        if not isinstance(end, int) or end < 0:
            raise ValueError(f"end must be a non-negative integer, got {end}")

        col = str(self.timestamp_column)

        if self.custom_template:
            # Use custom template with placeholder replacement
            sql = self.custom_template.replace("{column}", col)
            sql = sql.replace(self.start_placeholder, str(start))
            sql = sql.replace(self.end_placeholder, str(end))
        else:
            # Default BETWEEN expression
            sql = f"{col} >= {start} AND {col} <= {end}"

        return sql, {}

    def render_template_only(self, dialect: SQLDialect) -> str:
        """Render template with placeholders intact (for chunking logic).

        Args:
            dialect: SQL dialect for identifier quoting.

        Returns:
            SQL template string with placeholders preserved.
        """
        col = str(self.timestamp_column)

        if self.custom_template:
            return self.custom_template.replace("{column}", col)
        else:
            return (
                f"{col} >= {self.start_placeholder} AND {col} <= {self.end_placeholder}"
            )


class MinerConfig(BaseModel):
    """Structured configuration for SQL query mining operations.

    This class replaces the vulnerable MinerArgs model with a structured
    approach that eliminates SQL injection risks.

    Attributes:
        database: Optional database identifier.
        schema_name: Optional schema identifier.
        timestamp_column: Column for time-based filtering.
        range_expression: Structured range filtering.
        chunk_size: Records per processing chunk.
        current_marker: Current timestamp marker position.
        miner_start_time_epoch: Mining start time (epoch seconds).
    """

    database: Optional[Identifier] = Field(
        default=None, description="Target database identifier"
    )
    schema_name: Optional[Identifier] = Field(
        default=None, description="Target schema identifier"
    )
    timestamp_column: Identifier = Field(
        ..., description="Column containing timestamps for ordering"
    )
    range_expression: RangeExpression = Field(
        ..., description="Structured range filtering expression"
    )
    chunk_size: int = Field(
        default=5000, gt=0, description="Number of records per chunk"
    )
    current_marker: int = Field(
        default=0, ge=0, description="Current timestamp marker for processing"
    )
    miner_start_time_epoch: int = Field(
        default_factory=lambda: int((datetime.now() - timedelta(days=14)).timestamp()),
        ge=0,
        description="Mining start time in epoch seconds",
    )
    # Legacy field for backward compatibility with templates using {sql_replace_from}
    sql_replace_from: Optional[str] = Field(
        default=None,
        description="Legacy: Original SQL fragment to be replaced (for backward compatibility)",
    )

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Class variable for default epoch calculation
    _DEFAULT_DAYS_BACK: ClassVar[int] = 14

    @classmethod
    def from_legacy_miner_args(
        cls, miner_args: Dict[str, Any], emit_warning: bool = True
    ) -> MinerConfig:
        """Create MinerConfig from legacy MinerArgs dictionary.

        This adapter enables backward compatibility during migration.

        Args:
            miner_args: Legacy miner_args dictionary.
            emit_warning: Whether to emit deprecation warning.

        Returns:
            New MinerConfig instance.

        Raises:
            ValueError: If legacy args cannot be converted.
        """
        if emit_warning:
            warnings.warn(
                "Legacy miner_args format is deprecated. "
                "Please migrate to MinerConfig for improved security.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Extract identifiers
        database = None
        if miner_args.get("database_name_cleaned"):
            database = Identifier(value=miner_args["database_name_cleaned"])

        schema_name = None
        if miner_args.get("schema_name_cleaned"):
            schema_name = Identifier(value=miner_args["schema_name_cleaned"])

        timestamp_col = Identifier(value=miner_args["timestamp_column"])

        # Build RangeExpression from sql_replace_* fields
        range_expr = cls._build_range_expression(miner_args, timestamp_col)

        return cls(
            database=database,
            schema_name=schema_name,
            timestamp_column=timestamp_col,
            range_expression=range_expr,
            chunk_size=miner_args.get("chunk_size", 5000),
            current_marker=miner_args.get("current_marker", 0),
            miner_start_time_epoch=miner_args.get(
                "miner_start_time_epoch",
                int(
                    (
                        datetime.now() - timedelta(days=cls._DEFAULT_DAYS_BACK)
                    ).timestamp()
                ),
            ),
            sql_replace_from=miner_args.get("sql_replace_from"),
        )

    @classmethod
    def _build_range_expression(
        cls, miner_args: Dict[str, Any], timestamp_col: Identifier
    ) -> RangeExpression:
        """Build RangeExpression from legacy sql_replace_* fields."""
        sql_replace_to = miner_args.get("sql_replace_to", "")
        start_key = miner_args.get("ranged_sql_start_key", "[START_MARKER]")
        end_key = miner_args.get("ranged_sql_end_key", "[END_MARKER]")

        if sql_replace_to:
            # Use sql_replace_to as custom template
            return RangeExpression(
                timestamp_column=timestamp_col,
                start_placeholder=start_key,
                end_placeholder=end_key,
                custom_template=sql_replace_to,
            )
        else:
            # Default range expression
            return RangeExpression(
                timestamp_column=timestamp_col,
                start_placeholder=start_key,
                end_placeholder=end_key,
            )

    def format_query(
        self,
        template: str,
        dialect: SQLDialect,
        start_marker: Optional[int] = None,
        end_marker: Optional[int] = None,
    ) -> str:
        """Format a SQL template with validated values.

        Args:
            template: SQL template with placeholders.
            dialect: SQL dialect for quoting.
            start_marker: Range start value (if using ranges).
            end_marker: Range end value (if using ranges).

        Returns:
            Formatted SQL string.
        """
        # Build format arguments
        format_args: Dict[str, Any] = {
            "miner_start_time_epoch": self.miner_start_time_epoch,
            "chunk_size": self.chunk_size,
            "current_marker": self.current_marker,
            "timestamp_column": str(self.timestamp_column),
        }

        if self.database:
            format_args["database_name_cleaned"] = str(self.database)
        if self.schema_name:
            format_args["schema_name_cleaned"] = str(self.schema_name)

        # For legacy templates that use {sql_replace_from} and {sql_replace_to}
        sql_replace_to = self.range_expression.custom_template or ""
        if self.sql_replace_from:
            format_args["sql_replace_from"] = self.sql_replace_from
            format_args["sql_replace_to"] = sql_replace_to

        # Format the base template
        result = template.format(**format_args)

        # Legacy: Replace sql_replace_from with sql_replace_to (custom_template)
        if self.sql_replace_from and sql_replace_to:
            result = result.replace(self.sql_replace_from, sql_replace_to)

        # Handle range expression if markers provided
        if start_marker is not None and end_marker is not None:
            # Replace the placeholders with actual values
            result = result.replace(
                self.range_expression.start_placeholder, str(start_marker)
            )
            result = result.replace(
                self.range_expression.end_placeholder, str(end_marker)
            )

        return result

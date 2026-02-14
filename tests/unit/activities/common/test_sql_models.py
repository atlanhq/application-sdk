"""Unit tests for SQL models (Identifier, RangeExpression, MinerConfig).

These tests verify that the structured SQL models properly validate inputs
and prevent SQL injection attacks.
"""

import warnings
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from application_sdk.activities.common.sql_models import (
    Identifier,
    MinerConfig,
    RangeExpression,
    SQLDialect,
)


class TestIdentifier:
    """Unit tests for Identifier class."""

    def test_valid_simple_identifier(self) -> None:
        """Test valid simple identifiers."""
        ident = Identifier(value="ACCOUNT_USAGE")
        assert ident.value == "ACCOUNT_USAGE"
        assert str(ident) == "ACCOUNT_USAGE"

    def test_valid_identifier_with_underscore(self) -> None:
        """Test identifiers starting with underscore."""
        ident = Identifier(value="_my_table")
        assert ident.value == "_my_table"

    def test_valid_identifier_with_dollar(self) -> None:
        """Test identifiers with dollar sign."""
        ident = Identifier(value="table$1")
        assert ident.value == "table$1"

    def test_valid_identifier_with_numbers(self) -> None:
        """Test identifiers with numbers (not at start)."""
        ident = Identifier(value="table123")
        assert ident.value == "table123"

    def test_invalid_identifier_empty(self) -> None:
        """Reject empty identifiers."""
        with pytest.raises(ValidationError):
            Identifier(value="")

    def test_invalid_identifier_whitespace_only(self) -> None:
        """Reject whitespace-only identifiers."""
        with pytest.raises(ValidationError):
            Identifier(value="   ")

    def test_invalid_identifier_starts_with_number(self) -> None:
        """Reject identifiers starting with a number."""
        with pytest.raises(ValidationError):
            Identifier(value="123table")

    def test_invalid_identifier_with_spaces(self) -> None:
        """Reject identifiers with spaces."""
        with pytest.raises(ValidationError):
            Identifier(value="my table")

    def test_invalid_identifier_with_dash(self) -> None:
        """Reject identifiers with dashes."""
        with pytest.raises(ValidationError):
            Identifier(value="my-table")

    def test_render_generic_dialect(self) -> None:
        """Test generic dialect (no quoting)."""
        ident = Identifier(value="QUERY_HISTORY")
        assert ident.render(SQLDialect.GENERIC) == "QUERY_HISTORY"

    def test_render_snowflake_dialect(self) -> None:
        """Test Snowflake quoting (double quotes)."""
        ident = Identifier(value="QUERY_HISTORY")
        assert ident.render(SQLDialect.SNOWFLAKE) == '"QUERY_HISTORY"'

    def test_render_redshift_dialect(self) -> None:
        """Test Redshift quoting (double quotes)."""
        ident = Identifier(value="query_history")
        assert ident.render(SQLDialect.REDSHIFT) == '"query_history"'

    def test_render_postgres_dialect(self) -> None:
        """Test PostgreSQL quoting (double quotes)."""
        ident = Identifier(value="pg_stat_statements")
        assert ident.render(SQLDialect.POSTGRES) == '"pg_stat_statements"'

    def test_render_mysql_dialect(self) -> None:
        """Test MySQL quoting (backticks)."""
        ident = Identifier(value="query_history")
        assert ident.render(SQLDialect.MYSQL) == "`query_history`"

    def test_render_bigquery_dialect(self) -> None:
        """Test BigQuery quoting (backticks)."""
        ident = Identifier(value="JOBS_BY_PROJECT")
        assert ident.render(SQLDialect.BIGQUERY) == "`JOBS_BY_PROJECT`"

    def test_identifier_is_frozen(self) -> None:
        """Verify Identifier is immutable."""
        ident = Identifier(value="table")
        with pytest.raises(ValidationError):
            ident.value = "other_table"  # type: ignore


class TestRangeExpression:
    """Unit tests for RangeExpression class."""

    def test_default_range_expression(self) -> None:
        """Test default BETWEEN expression without custom template."""
        expr = RangeExpression(timestamp_column=Identifier(value="start_time"))
        sql, _ = expr.render(SQLDialect.GENERIC, start=100, end=200)
        assert sql == "start_time >= 100 AND start_time <= 200"

    def test_default_placeholders(self) -> None:
        """Test default placeholder values."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        assert expr.start_placeholder == "[START_MARKER]"
        assert expr.end_placeholder == "[END_MARKER]"

    def test_custom_template_valid(self) -> None:
        """Test valid custom template."""
        template = (
            "CAST(EXTRACT(epoch from {column}) as INT8) * 1000 >= [START_MARKER] "
            "AND CAST(EXTRACT(epoch from {column}) as INT8) * 1000 < [END_MARKER]"
        )
        expr = RangeExpression(
            timestamp_column=Identifier(value="starttime"),
            custom_template=template,
        )
        assert expr.custom_template == template

    def test_custom_template_render(self) -> None:
        """Test rendering custom template with values."""
        template = "{column} >= [START_MARKER] AND {column} < [END_MARKER]"
        expr = RangeExpression(
            timestamp_column=Identifier(value="ts"),
            custom_template=template,
        )
        sql, _ = expr.render(SQLDialect.GENERIC, start=100, end=200)
        assert sql == "ts >= 100 AND ts < 200"

    def test_custom_template_missing_start_placeholder(self) -> None:
        """Reject template missing start placeholder."""
        with pytest.raises(ValidationError) as exc_info:
            RangeExpression(
                timestamp_column=Identifier(value="ts"),
                custom_template="col >= 100 AND col < [END_MARKER]",
            )
        assert "start_placeholder" in str(exc_info.value)

    def test_custom_template_missing_end_placeholder(self) -> None:
        """Reject template missing end placeholder."""
        with pytest.raises(ValidationError) as exc_info:
            RangeExpression(
                timestamp_column=Identifier(value="ts"),
                custom_template="col >= [START_MARKER] AND col < 200",
            )
        assert "end_placeholder" in str(exc_info.value)

    def test_custom_placeholder_values(self) -> None:
        """Test custom placeholder strings."""
        expr = RangeExpression(
            timestamp_column=Identifier(value="ts"),
            start_placeholder="{BEGIN}",
            end_placeholder="{FINISH}",
            custom_template="ts >= {BEGIN} AND ts < {FINISH}",
        )
        sql, _ = expr.render(SQLDialect.GENERIC, start=100, end=200)
        assert sql == "ts >= 100 AND ts < 200"

    def test_render_template_only(self) -> None:
        """Test rendering template with placeholders intact."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        template = expr.render_template_only(SQLDialect.GENERIC)
        assert template == "ts >= [START_MARKER] AND ts <= [END_MARKER]"

    def test_render_with_negative_start(self) -> None:
        """Reject negative start value."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises(ValueError) as exc_info:
            expr.render(SQLDialect.GENERIC, start=-1, end=100)
        assert "non-negative" in str(exc_info.value)

    def test_render_with_negative_end(self) -> None:
        """Reject negative end value."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises(ValueError) as exc_info:
            expr.render(SQLDialect.GENERIC, start=0, end=-1)
        assert "non-negative" in str(exc_info.value)

    def test_render_with_non_integer_start(self) -> None:
        """Reject non-integer start value."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises(ValueError):
            expr.render(SQLDialect.GENERIC, start="100", end=200)  # type: ignore

    def test_invalid_placeholder_with_semicolon(self) -> None:
        """Reject placeholder with semicolon."""
        with pytest.raises(ValidationError):
            RangeExpression(
                timestamp_column=Identifier(value="ts"),
                start_placeholder="[START;DROP]",
            )


class TestMinerConfig:
    """Unit tests for MinerConfig class."""

    @pytest.fixture
    def base_config_dict(self) -> Dict[str, Any]:
        """Provide a valid MinerConfig dictionary."""
        return {
            "database": Identifier(value="SNOWFLAKE"),
            "schema_name": Identifier(value="ACCOUNT_USAGE"),
            "timestamp_column": Identifier(value="START_TIME"),
            "range_expression": RangeExpression(
                timestamp_column=Identifier(value="START_TIME"),
                custom_template="ts >= [START_MARKER] AND ts < [END_MARKER]",
            ),
            "chunk_size": 5000,
            "current_marker": 0,
        }

    def test_valid_config(self, base_config_dict: Dict[str, Any]) -> None:
        """Test valid MinerConfig instantiation."""
        config = MinerConfig(**base_config_dict)
        assert config.database is not None
        assert config.database.value == "SNOWFLAKE"
        assert config.chunk_size == 5000

    def test_optional_database(self) -> None:
        """Test MinerConfig without database."""
        config = MinerConfig(
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(timestamp_column=Identifier(value="ts")),
        )
        assert config.database is None

    def test_optional_schema(self) -> None:
        """Test MinerConfig without schema."""
        config = MinerConfig(
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(timestamp_column=Identifier(value="ts")),
        )
        assert config.schema_name is None

    def test_default_chunk_size(self) -> None:
        """Test default chunk_size value."""
        config = MinerConfig(
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(timestamp_column=Identifier(value="ts")),
        )
        assert config.chunk_size == 5000

    def test_default_current_marker(self) -> None:
        """Test default current_marker value."""
        config = MinerConfig(
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(timestamp_column=Identifier(value="ts")),
        )
        assert config.current_marker == 0

    def test_invalid_chunk_size_zero(self) -> None:
        """Reject chunk_size of zero."""
        with pytest.raises(ValidationError):
            MinerConfig(
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
                chunk_size=0,
            )

    def test_invalid_chunk_size_negative(self) -> None:
        """Reject negative chunk_size."""
        with pytest.raises(ValidationError):
            MinerConfig(
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
                chunk_size=-1,
            )

    def test_invalid_current_marker_negative(self) -> None:
        """Reject negative current_marker."""
        with pytest.raises(ValidationError):
            MinerConfig(
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
                current_marker=-1,
            )

    def test_format_query_basic(self) -> None:
        """Test basic query formatting."""
        config = MinerConfig(
            database=Identifier(value="mydb"),
            schema_name=Identifier(value="myschema"),
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(timestamp_column=Identifier(value="ts")),
            miner_start_time_epoch=1700000000,
        )
        template = "SELECT * FROM {database_name_cleaned}.{schema_name_cleaned}"
        result = config.format_query(template, SQLDialect.GENERIC)
        assert result == "SELECT * FROM mydb.myschema"

    def test_format_query_with_markers(self) -> None:
        """Test query formatting with range markers."""
        config = MinerConfig(
            timestamp_column=Identifier(value="ts"),
            range_expression=RangeExpression(
                timestamp_column=Identifier(value="ts"),
                custom_template="ts >= [START_MARKER] AND ts < [END_MARKER]",
            ),
        )
        template = (
            "SELECT * FROM table WHERE ts >= [START_MARKER] AND ts < [END_MARKER]"
        )
        result = config.format_query(
            template, SQLDialect.GENERIC, start_marker=100, end_marker=200
        )
        assert "ts >= 100" in result
        assert "ts < 200" in result

    def test_extra_fields_forbidden(self) -> None:
        """Verify extra fields are rejected."""
        with pytest.raises(ValidationError):
            MinerConfig(
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
                unknown_field="value",  # type: ignore
            )


class TestBackwardCompatibility:
    """Tests for legacy MinerArgs compatibility."""

    @pytest.fixture
    def legacy_miner_args(self) -> Dict[str, Any]:
        """Provide a valid legacy miner_args dictionary."""
        return {
            "database_name_cleaned": "SNOWFLAKE",
            "schema_name_cleaned": "ACCOUNT_USAGE",
            "timestamp_column": "START_TIME",
            "chunk_size": 5000,
            "current_marker": 0,
            "sql_replace_from": "condition",
            "sql_replace_to": "ts >= [START_MARKER] AND ts < [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
            "miner_start_time_epoch": 1700000000,
        }

    def test_convert_simple_legacy_args(
        self, legacy_miner_args: Dict[str, Any]
    ) -> None:
        """Test basic conversion from legacy format."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = MinerConfig.from_legacy_miner_args(legacy_miner_args)

            # Verify deprecation warning was emitted
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()

        # Verify conversion
        assert config.database is not None
        assert config.database.value == "SNOWFLAKE"
        assert config.schema_name is not None
        assert config.schema_name.value == "ACCOUNT_USAGE"
        assert config.chunk_size == 5000

    def test_convert_without_warning(self, legacy_miner_args: Dict[str, Any]) -> None:
        """Test conversion with warning suppressed."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = MinerConfig.from_legacy_miner_args(
                legacy_miner_args, emit_warning=False
            )
            assert len(w) == 0

        assert config.database is not None
        assert config.database.value == "SNOWFLAKE"

    def test_convert_without_database(self) -> None:
        """Test conversion without database."""
        legacy = {
            "timestamp_column": "ts",
            "chunk_size": 100,
            "current_marker": 0,
            "sql_replace_from": "",
            "sql_replace_to": "",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        config = MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)
        assert config.database is None

    def test_convert_preserves_custom_template(
        self, legacy_miner_args: Dict[str, Any]
    ) -> None:
        """Test that sql_replace_to becomes custom_template."""
        config = MinerConfig.from_legacy_miner_args(
            legacy_miner_args, emit_warning=False
        )
        assert config.range_expression.custom_template is not None
        assert "[START_MARKER]" in config.range_expression.custom_template
        assert "[END_MARKER]" in config.range_expression.custom_template

    def test_convert_with_invalid_identifier(self) -> None:
        """Ensure conversion fails for invalid legacy identifiers."""
        legacy = {
            "database_name_cleaned": "db; DROP TABLE",
            "timestamp_column": "ts",
            "sql_replace_from": "",
            "sql_replace_to": "",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        with pytest.raises(ValidationError):
            MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)

    def test_convert_with_empty_sql_replace_to(self) -> None:
        """Test conversion with empty sql_replace_to (uses default range)."""
        legacy = {
            "timestamp_column": "ts",
            "chunk_size": 100,
            "current_marker": 0,
            "sql_replace_from": "",
            "sql_replace_to": "",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        config = MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)
        # Should use default range expression (no custom template)
        assert config.range_expression.custom_template is None

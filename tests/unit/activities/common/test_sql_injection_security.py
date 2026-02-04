"""Security tests for SQL injection prevention.

These tests verify that the structured SQL models properly block
all known SQL injection attack patterns.
"""

import pytest
from pydantic import ValidationError

from application_sdk.activities.common.sql_models import (
    Identifier,
    MinerConfig,
    RangeExpression,
    SQLDialect,
)


class TestIdentifierSQLInjection:
    """Security tests for Identifier SQL injection prevention."""

    @pytest.mark.parametrize(
        "malicious_input",
        [
            # Statement terminators
            "users; DROP TABLE users;",
            "schema; DELETE FROM secrets;",
            "table;--",
            # Comment injection
            "table--comment",
            "table/* inline comment */name",
            "col/**/name",
            # Quote escaping
            "table'name",
            'table"name',
            "table`name",
            # SQL keywords with spaces
            "table OR 1=1",
            "col AND true",
            # Special characters
            "table\nname",
            "table\r\nname",
            "table\x00name",
            # Unicode tricks
            "table\u0000name",
            # Parentheses
            "table()",
            "(SELECT * FROM users)",
            # Equals and comparisons
            "col=1",
            "table<>",
        ],
    )
    def test_identifier_blocks_injection(self, malicious_input: str) -> None:
        """Ensure Identifier blocks all injection attempts."""
        with pytest.raises(ValidationError):
            Identifier(value=malicious_input)

    @pytest.mark.parametrize(
        "valid_input",
        [
            "users",
            "ACCOUNT_USAGE",
            "_private_table",
            "table123",
            "my_table_v2",
            "Table$Special",
            "T",
            "_",
        ],
    )
    def test_identifier_allows_valid(self, valid_input: str) -> None:
        """Ensure Identifier allows valid identifiers."""
        ident = Identifier(value=valid_input)
        assert ident.value == valid_input


class TestRangeExpressionSQLInjection:
    """Security tests for RangeExpression SQL injection prevention."""

    @pytest.mark.parametrize(
        "malicious_template",
        [
            # Statement terminators
            "[START_MARKER]; DROP TABLE users; [END_MARKER]",
            "[START_MARKER]; DELETE FROM secrets WHERE 1=1; [END_MARKER]",
            "ts >= [START_MARKER]; INSERT INTO logs VALUES('hacked'); --[END_MARKER]",
            # SQL comment injection
            "[START_MARKER]-- comment\n[END_MARKER]",
            "[START_MARKER]/* block comment */[END_MARKER]",
            "ts >= [START_MARKER] /*injected*/ AND ts < [END_MARKER]",
            # UNION-based injection
            "[START_MARKER] UNION SELECT * FROM passwords WHERE x=[END_MARKER]",
            "ts >= [START_MARKER] UNION ALL SELECT password FROM users--[END_MARKER]",
            # Subquery injection
            "ts >= [START_MARKER] AND (SELECT COUNT(*) FROM users) > 0 AND ts < [END_MARKER]",
            # DDL/DML injection
            "[START_MARKER] DROP TABLE users [END_MARKER]",
            "[START_MARKER] CREATE TABLE hacked(data TEXT) [END_MARKER]",
            "[START_MARKER] ALTER TABLE users ADD COLUMN hacked TEXT [END_MARKER]",
            "[START_MARKER] GRANT ALL ON users TO attacker [END_MARKER]",
            "[START_MARKER] TRUNCATE TABLE users [END_MARKER]",
            "[START_MARKER] INSERT INTO users VALUES('hacked') [END_MARKER]",
            "[START_MARKER] UPDATE users SET password='hacked' [END_MARKER]",
            "[START_MARKER] DELETE FROM users [END_MARKER]",
            # EXEC injection (SQL Server style)
            "[START_MARKER] EXEC xp_cmdshell 'dir' [END_MARKER]",
        ],
    )
    def test_range_expression_blocks_injection_in_template(
        self, malicious_template: str
    ) -> None:
        """Ensure RangeExpression blocks injection in templates."""
        with pytest.raises(ValidationError):
            RangeExpression(
                timestamp_column=Identifier(value="ts"),
                custom_template=malicious_template,
            )

    @pytest.mark.parametrize(
        "valid_template",
        [
            "ts >= [START_MARKER] AND ts < [END_MARKER]",
            "CAST(EXTRACT(epoch from {column}) as INT8) * 1000 >= [START_MARKER] AND CAST(EXTRACT(epoch from {column}) as INT8) * 1000 < [END_MARKER]",
            "{column} BETWEEN [START_MARKER] AND [END_MARKER]",
            "TO_TIMESTAMP_TZ([START_MARKER], 3) <= {column} AND {column} < TO_TIMESTAMP_TZ([END_MARKER], 3)",
        ],
    )
    def test_range_expression_allows_valid_templates(self, valid_template: str) -> None:
        """Ensure RangeExpression allows valid templates."""
        expr = RangeExpression(
            timestamp_column=Identifier(value="ts"),
            custom_template=valid_template,
        )
        assert expr.custom_template == valid_template


class TestMarkerValueSQLInjection:
    """Security tests for marker value SQL injection prevention."""

    @pytest.mark.parametrize(
        "malicious_start",
        [
            "0 OR 1=1",
            "100; DROP TABLE",
            "200--comment",
            "300/*block*/",
            "0) OR (1=1",
            "1' OR '1'='1",
            "-1 UNION SELECT",
        ],
    )
    def test_render_rejects_non_numeric_start(self, malicious_start: str) -> None:
        """Ensure render rejects non-numeric start values."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises((ValueError, TypeError)):
            expr.render(SQLDialect.GENERIC, start=malicious_start, end=100)  # type: ignore

    @pytest.mark.parametrize(
        "malicious_end",
        [
            "0 OR 1=1",
            "100; DROP TABLE",
            "200--comment",
            "300/*block*/",
            "0) OR (1=1",
            "1' OR '1'='1",
            "-1 UNION SELECT",
        ],
    )
    def test_render_rejects_non_numeric_end(self, malicious_end: str) -> None:
        """Ensure render rejects non-numeric end values."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises((ValueError, TypeError)):
            expr.render(SQLDialect.GENERIC, start=100, end=malicious_end)  # type: ignore

    def test_render_rejects_negative_start(self) -> None:
        """Ensure render rejects negative start values."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises(ValueError) as exc_info:
            expr.render(SQLDialect.GENERIC, start=-1, end=100)
        assert "non-negative" in str(exc_info.value)

    def test_render_rejects_negative_end(self) -> None:
        """Ensure render rejects negative end values."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        with pytest.raises(ValueError) as exc_info:
            expr.render(SQLDialect.GENERIC, start=0, end=-1)
        assert "non-negative" in str(exc_info.value)

    def test_render_accepts_valid_markers(self) -> None:
        """Ensure render accepts valid numeric markers."""
        expr = RangeExpression(timestamp_column=Identifier(value="ts"))
        sql, _ = expr.render(SQLDialect.GENERIC, start=1700000000, end=1700000100)
        assert "1700000000" in sql
        assert "1700000100" in sql


class TestMinerConfigSQLInjection:
    """Security tests for MinerConfig SQL injection prevention."""

    def test_config_rejects_invalid_database(self) -> None:
        """Ensure MinerConfig rejects invalid database identifiers."""
        with pytest.raises(ValidationError):
            MinerConfig(
                database=Identifier(value="db; DROP TABLE"),
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
            )

    def test_config_rejects_invalid_schema(self) -> None:
        """Ensure MinerConfig rejects invalid schema identifiers."""
        with pytest.raises(ValidationError):
            MinerConfig(
                schema_name=Identifier(value="schema; DROP TABLE"),
                timestamp_column=Identifier(value="ts"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
            )

    def test_config_rejects_invalid_timestamp_column(self) -> None:
        """Ensure MinerConfig rejects invalid timestamp column."""
        with pytest.raises(ValidationError):
            MinerConfig(
                timestamp_column=Identifier(value="col; DROP TABLE"),
                range_expression=RangeExpression(
                    timestamp_column=Identifier(value="ts")
                ),
            )

    def test_legacy_conversion_rejects_invalid_database(self) -> None:
        """Ensure legacy conversion rejects invalid database."""
        legacy = {
            "database_name_cleaned": "db; DROP TABLE users",
            "timestamp_column": "ts",
            "sql_replace_to": "[START_MARKER] [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        with pytest.raises(ValidationError):
            MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)

    def test_legacy_conversion_rejects_invalid_schema(self) -> None:
        """Ensure legacy conversion rejects invalid schema."""
        legacy = {
            "schema_name_cleaned": "schema; DROP TABLE users",
            "timestamp_column": "ts",
            "sql_replace_to": "[START_MARKER] [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        with pytest.raises(ValidationError):
            MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)

    def test_legacy_conversion_rejects_unsafe_template(self) -> None:
        """Ensure legacy conversion rejects unsafe sql_replace_to."""
        legacy = {
            "timestamp_column": "ts",
            "sql_replace_to": "[START_MARKER]; DROP TABLE users; [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        }
        with pytest.raises(ValidationError):
            MinerConfig.from_legacy_miner_args(legacy, emit_warning=False)


class TestRealWorldAttackPatterns:
    """Tests for real-world SQL injection attack patterns."""

    def test_redshift_epoch_extraction_attack(self) -> None:
        """Test attack attempting to exploit Redshift epoch extraction."""
        malicious = (
            "CAST(EXTRACT(epoch from starttime) as INT8) * 1000 >= [START_MARKER]; "
            "DROP TABLE stl_query; -- [END_MARKER]"
        )
        with pytest.raises(ValidationError):
            RangeExpression(
                timestamp_column=Identifier(value="starttime"),
                custom_template=malicious,
            )

    def test_snowflake_timestamp_attack(self) -> None:
        """Test attack attempting to exploit Snowflake timestamp functions."""
        malicious = (
            "TO_TIMESTAMP_TZ([START_MARKER], 3) <= SESSION_CREATED_ON; "
            "INSERT INTO QUERY_HISTORY SELECT * FROM SECRETS; -- [END_MARKER]"
        )
        with pytest.raises(ValidationError):
            RangeExpression(
                timestamp_column=Identifier(value="SESSION_CREATED_ON"),
                custom_template=malicious,
            )

    def test_boolean_based_blind_injection(self) -> None:
        """Test boolean-based blind SQL injection in identifiers."""
        with pytest.raises(ValidationError):
            Identifier(value="users WHERE 1=1 OR username")

    def test_stacked_queries_attack(self) -> None:
        """Test stacked queries attack."""
        malicious = (
            "[START_MARKER]; "
            "CREATE USER attacker WITH PASSWORD 'pwned'; "
            "GRANT ALL PRIVILEGES TO attacker; "
            "[END_MARKER]"
        )
        with pytest.raises(ValidationError):
            RangeExpression(
                timestamp_column=Identifier(value="ts"),
                custom_template=malicious,
            )

    def test_valid_redshift_template(self) -> None:
        """Ensure valid Redshift-style template is accepted."""
        valid = (
            "CAST(EXTRACT(epoch from {column}) as INT8) * 1000 >= [START_MARKER] "
            "AND CAST(EXTRACT(epoch from {column}) as INT8) * 1000 < [END_MARKER]"
        )
        expr = RangeExpression(
            timestamp_column=Identifier(value="starttime"),
            custom_template=valid,
        )
        sql, _ = expr.render(SQLDialect.REDSHIFT, start=1700000000, end=1700000100)
        assert "1700000000" in sql
        assert "1700000100" in sql
        assert "starttime" in sql

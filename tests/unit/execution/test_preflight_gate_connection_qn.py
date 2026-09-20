"""Tests for connection_qualified_name validation in the preflight gate.

Validates the platform-level check that rejects a broken connection snapshot
(missing qualifiedName) before any extraction work begins.
"""

from __future__ import annotations

from application_sdk.execution._temporal.preflight_gate import (
    _check_connection_qualified_name,
)


class TestCheckConnectionQualifiedName:
    """Unit tests for _check_connection_qualified_name."""

    def test_no_connection_data_returns_none(self) -> None:
        """Snapshot without connection or connection_qualified_name — skip."""
        result = _check_connection_qualified_name({"some_other_field": "value"})
        assert result is None

    def test_empty_snapshot_returns_none(self) -> None:
        """Empty snapshot — skip."""
        result = _check_connection_qualified_name({})
        assert result is None

    def test_valid_top_level_cqn_returns_none(self) -> None:
        """Non-empty connection_qualified_name — passes."""
        result = _check_connection_qualified_name(
            {"connection_qualified_name": "default/looker/1789393939"}
        )
        assert result is None

    def test_valid_attribute_qn_returns_none(self) -> None:
        """qualifiedName in connection.attributes — passes."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {
                        "qualifiedName": "default/looker/1789393939",
                        "defaultCredentialGuid": "abc-123",
                    },
                },
            }
        )
        assert result is None

    def test_empty_cqn_with_connection_missing_qn_fails(self) -> None:
        """The CONNECT-1738 scenario: connection exists but no qualifiedName."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {
                        "defaultCredentialGuid": "cfd33802-a2ac-4031-a7d3-240d8d4717b0",
                    },
                },
            }
        )
        assert result is not None
        assert result.passed is False
        assert result.name == "connection_qualified_name"
        assert result.error is not None

    def test_empty_cqn_no_connection_object_fails(self) -> None:
        """connection_qualified_name is explicitly empty string — still fails."""
        result = _check_connection_qualified_name({"connection_qualified_name": ""})
        assert result is not None
        assert result.passed is False

    def test_whitespace_only_cqn_fails(self) -> None:
        """Whitespace-only qualified name is treated as empty."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "   ",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {},
                },
            }
        )
        assert result is not None
        assert result.passed is False

    def test_connection_with_snake_case_qualified_name_passes(self) -> None:
        """Some connectors use qualified_name (snake_case) in attributes."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {
                        "qualified_name": "default/databricks/1789000000",
                    },
                },
            }
        )
        assert result is None

    def test_connection_none_attributes_fails(self) -> None:
        """Connection with None attributes — treated as missing QN."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": None,
                },
            }
        )
        assert result is not None
        assert result.passed is False

    def test_valid_cqn_overrides_missing_attribute_qn(self) -> None:
        """Top-level connection_qualified_name is sufficient even without attr."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "default/looker/1789393939",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {},
                },
            }
        )
        assert result is None

    def test_error_has_invalid_input_category(self) -> None:
        """Failed check carries an INVALID_INPUT typed error."""
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {"defaultCredentialGuid": "abc"},
                },
            }
        )
        assert result is not None
        assert result.error is not None
        assert result.error.category.value == "INVALID_INPUT"

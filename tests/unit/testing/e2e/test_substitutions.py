"""Unit tests for the typed MustacheSubstitutions hierarchy.

Regression-guards the alias contract: field aliases must exactly match
the mustache literals the seed-DAG walker looks up (whole ``{{...}}``
strings as dict keys). Any alias drift would silently leave placeholders
un-substituted in the manifest args, causing AE to hang on the literal
string.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.substitutions import (
    MustacheSubstitutions,
    SQLMustacheSubstitutions,
)


def _make_connection_ref() -> ConnectionRef:
    return ConnectionRef.model_validate(
        {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/mysql/test-123",
                "name": "test-conn",
                "connectorName": "mysql",
                "adminUsers": [],
                "adminGroups": [],
                "adminRoles": [],
            },
        }
    )


class TestMustacheSubstitutionsAliases:
    """model_dump(by_alias=True) must produce exact {{...}}-keyed dict."""

    def test_base_keys_are_mustache_literals(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        dumped = subs.model_dump(by_alias=True)
        assert "{{credential}}" in dumped
        assert "{{credential-guid}}" in dumped
        assert "{{connection}}" in dumped
        # No unknown keys
        assert set(dumped.keys()) == {
            "{{credential}}",
            "{{credential-guid}}",
            "{{connection}}",
        }

    def test_credential_guid_default(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        dumped = subs.model_dump(by_alias=True)
        # AE runtime-substitutes this literal; the harness must NOT fill it in.
        assert dumped["{{credential-guid}}"] == "{{credentialGuid}}"

    def test_credential_defaults_none(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        dumped = subs.model_dump(by_alias=True)
        assert dumped["{{credential}}"] is None

    def test_connection_serialises_to_dict(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        dumped = subs.model_dump(by_alias=True)
        conn_val = dumped["{{connection}}"]
        assert isinstance(conn_val, dict)
        assert conn_val["typeName"] == "Connection"
        assert "attributes" in conn_val

    def test_connection_attributes_are_camel_case(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        dumped = subs.model_dump(by_alias=True)
        attrs = dumped["{{connection}}"]["attributes"]
        assert "qualifiedName" in attrs
        assert "adminUsers" in attrs

    def test_frozen_model(self) -> None:
        conn = _make_connection_ref()
        subs = MustacheSubstitutions(connection=conn)
        with pytest.raises(Exception):
            subs.connection = _make_connection_ref()  # type: ignore[misc]


class TestSQLMustacheSubstitutionsAliases:
    """SQLMustacheSubstitutions extends base with SQL mustache keys."""

    def _make_sql_subs(
        self, agent_json: dict[str, Any] | None = None
    ) -> SQLMustacheSubstitutions:
        return SQLMustacheSubstitutions(
            connection=_make_connection_ref(),
            extraction_method="agent",
            agent_json=agent_json,
            include_filter='{"^def$":[".*"]}',
            exclude_filter="{}",
            exclude_table_regex="",
            preflight_check=True,
        )

    def test_sql_keys_present(self) -> None:
        subs = self._make_sql_subs()
        dumped = subs.model_dump(by_alias=True)
        assert "{{extraction-method}}" in dumped
        assert "{{agent-json}}" in dumped
        assert "{{include-filter}}" in dumped
        assert "{{exclude-filter}}" in dumped
        assert "{{exclude-table-regex}}" in dumped
        assert "{{preflight-check}}" in dumped

    def test_sql_inherits_base_keys(self) -> None:
        subs = self._make_sql_subs()
        dumped = subs.model_dump(by_alias=True)
        assert "{{credential}}" in dumped
        assert "{{credential-guid}}" in dumped
        assert "{{connection}}" in dumped

    def test_extraction_method_value(self) -> None:
        subs = self._make_sql_subs()
        dumped = subs.model_dump(by_alias=True)
        assert dumped["{{extraction-method}}"] == "agent"

    def test_agent_json_none_by_default(self) -> None:
        subs = self._make_sql_subs()
        dumped = subs.model_dump(by_alias=True)
        assert dumped["{{agent-json}}"] is None

    def test_agent_json_populated(self) -> None:
        aj = {"host": "db.example.com", "port": 3306}
        subs = self._make_sql_subs(agent_json=aj)
        dumped = subs.model_dump(by_alias=True)
        assert dumped["{{agent-json}}"] == aj

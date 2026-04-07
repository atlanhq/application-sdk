"""Unit tests for handler contracts."""

import pytest
from pydantic import ValidationError

from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    Credential,
    MetadataField,
    MetadataObject,
    MetadataOutput,
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)


class TestCredential:
    def test_frozen(self):
        cred = Credential(key="api_key", value="secret")
        with pytest.raises((AttributeError, TypeError, ValidationError)):
            cred.key = "other"  # type: ignore[misc]

    def test_fields(self):
        cred = Credential(key="token", value="abc123")
        assert cred.key == "token"
        assert cred.value == "abc123"


class TestAuthStatus:
    def test_values(self):
        assert AuthStatus.SUCCESS == "success"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.EXPIRED == "expired"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


class TestAuthInput:
    def test_defaults(self):
        inp = AuthInput()
        assert inp.credentials == []
        assert inp.connection_id == ""
        assert inp.timeout_seconds == 30

    def test_with_credentials(self):
        creds = [Credential(key="k", value="v")]
        inp = AuthInput(credentials=creds, connection_id="conn-1", timeout_seconds=60)
        assert len(inp.credentials) == 1
        assert inp.timeout_seconds == 60


class TestAuthOutput:
    def test_required_status(self):
        out = AuthOutput(status=AuthStatus.SUCCESS)
        assert out.status == AuthStatus.SUCCESS
        assert out.message == ""
        assert out.identities == []
        assert out.scopes == []
        assert out.expires_at == ""

    def test_failed_status(self):
        out = AuthOutput(status=AuthStatus.FAILED, message="Bad credentials")
        assert out.status == AuthStatus.FAILED
        assert out.message == "Bad credentials"


class TestPreflightStatus:
    def test_values(self):
        assert PreflightStatus.READY == "ready"
        assert PreflightStatus.NOT_READY == "not_ready"
        assert PreflightStatus.PARTIAL == "partial"


class TestPreflightCheck:
    def test_defaults(self):
        check = PreflightCheck(name="connectivity")
        assert check.name == "connectivity"
        assert check.passed is False
        assert check.message == ""
        assert check.duration_ms == 0.0

    def test_passed(self):
        check = PreflightCheck(name="connectivity", passed=True, duration_ms=50.0)
        assert check.passed is True
        assert check.duration_ms == 50.0


class TestPreflightOutput:
    def test_required_status(self):
        out = PreflightOutput(status=PreflightStatus.READY)
        assert out.status == PreflightStatus.READY
        assert out.checks == []

    def test_with_checks(self):
        checks = [
            PreflightCheck(name="conn", passed=True),
            PreflightCheck(name="perms", passed=False, message="No read access"),
        ]
        out = PreflightOutput(status=PreflightStatus.PARTIAL, checks=checks)
        assert len(out.checks) == 2


class TestMetadataOutput:
    """Tests for the MetadataOutput hierarchy."""

    def test_base_class_is_empty(self):
        out = MetadataOutput()
        assert isinstance(out, MetadataOutput)

    def test_sql_output_defaults(self):
        out = SqlMetadataOutput()
        assert isinstance(out, MetadataOutput)
        assert out.objects == []

    def test_sql_output_with_objects(self):
        out = SqlMetadataOutput(objects=[
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"),
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"),
        ])
        assert len(out.objects) == 2
        assert out.objects[0].TABLE_CATALOG == "DEFAULT"
        assert out.objects[0].TABLE_SCHEMA == "FINANCE"

    def test_sql_output_model_dump(self):
        obj = SqlMetadataObject(TABLE_CATALOG="DB", TABLE_SCHEMA="SCH")
        assert obj.model_dump() == {"TABLE_CATALOG": "DB", "TABLE_SCHEMA": "SCH"}

    def test_api_output_defaults(self):
        out = ApiMetadataOutput()
        assert isinstance(out, MetadataOutput)
        assert out.objects == []

    def test_api_output_with_flat_objects(self):
        out = ApiMetadataOutput(objects=[
            ApiMetadataObject(value="t1", title="Tag 1", node_type="tag"),
        ])
        assert len(out.objects) == 1
        assert out.objects[0].value == "t1"
        assert out.objects[0].title == "Tag 1"
        assert out.objects[0].node_type == "tag"
        assert out.objects[0].children == []

    def test_api_output_nested_children(self):
        child = ApiMetadataObject(value="c1", title="Child 1")
        parent = ApiMetadataObject(
            value="p1", title="Parent", node_type="project", children=[child],
        )
        out = ApiMetadataOutput(objects=[parent])
        assert len(out.objects[0].children) == 1
        assert out.objects[0].children[0].value == "c1"

    def test_api_output_model_dump_nested(self):
        obj = ApiMetadataObject(
            value="p1", title="Parent", node_type="folder",
            children=[ApiMetadataObject(value="c1", title="Child")],
        )
        dumped = obj.model_dump()
        assert dumped == {
            "value": "p1",
            "title": "Parent",
            "node_type": "folder",
            "children": [
                {"value": "c1", "title": "Child", "node_type": "", "children": []},
            ],
        }

    def test_isinstance_both_subtypes(self):
        sql = SqlMetadataOutput()
        api = ApiMetadataOutput()
        assert isinstance(sql, MetadataOutput)
        assert isinstance(api, MetadataOutput)

    def test_legacy_metadata_object_still_works(self):
        """Deprecated MetadataObject is still importable and functional."""
        obj = MetadataObject(
            name="users",
            object_type="TABLE",
            schema="public",
            fields=[MetadataField(name="id", field_type="INTEGER")],
        )
        assert obj.name == "users"
        assert obj.fields[0].name == "id"

"""Unit tests for handler contracts."""

import pytest

from application_sdk.handler.contracts import (
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
)


class TestCredential:
    def test_frozen(self):
        cred = Credential(key="api_key", value="secret")
        with pytest.raises((AttributeError, TypeError)):
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
    def test_defaults(self):
        out = MetadataOutput()
        assert out.objects == []
        assert out.total_count == 0
        assert out.truncated is False
        assert out.fetch_duration_ms == 0.0

    def test_with_objects(self):
        obj = MetadataObject(
            name="users",
            object_type="TABLE",
            schema="public",
            fields=[MetadataField(name="id", field_type="INTEGER")],
        )
        out = MetadataOutput(objects=[obj], total_count=1)
        assert out.total_count == 1
        assert out.objects[0].name == "users"
        assert out.objects[0].fields[0].name == "id"

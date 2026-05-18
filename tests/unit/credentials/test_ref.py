"""Unit tests for CredentialRef, factory functions, and CredentialRef.resolve()."""

import pytest
from pydantic import ValidationError

from application_sdk.credentials.errors import (
    CredentialResolvableTypeError,
    CredentialRoutingError,
)
from application_sdk.credentials.ref import (
    CredentialRef,
    CredentialResolvable,
    api_key_ref,
    atlan_api_token_ref,
    atlan_oauth_client_ref,
    basic_ref,
    bearer_token_ref,
    certificate_ref,
    git_ssh_ref,
    git_token_ref,
    legacy_credential_ref,
    oauth_client_ref,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.templates.contracts.sql_metadata import ExtractionInput


class TestCredentialRefConstruction:
    def test_minimal_construction(self):
        ref = CredentialRef(name="my-cred", credential_type="api_key")
        assert ref.name == "my-cred"
        assert ref.credential_type == "api_key"
        assert ref.store_name == "default"
        assert ref.credential_guid == ""

    def test_full_construction(self):
        ref = CredentialRef(
            name="my-cred",
            credential_type="basic",
            store_name="prod-store",
            credential_guid="abc-123",
        )
        assert ref.credential_guid == "abc-123"
        assert ref.store_name == "prod-store"

    def test_frozen(self):
        ref = CredentialRef(name="x", credential_type="api_key")
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            ref.name = "y"  # type: ignore[misc]

    def test_repr_is_safe(self):
        ref = CredentialRef(name="my-cred", credential_type="api_key")
        r = repr(ref)
        assert "my-cred" in r
        assert "api_key" in r
        # credential_guid deliberately omitted from repr
        assert "credential_guid" not in r

    def test_equality(self):
        a = CredentialRef(name="x", credential_type="api_key")
        b = CredentialRef(name="x", credential_type="api_key")
        assert a == b

    def test_hashable(self):
        ref = CredentialRef(name="x", credential_type="api_key")
        # Frozen dataclasses are hashable
        assert hash(ref) is not None
        s = {ref}
        assert ref in s


class TestFactoryFunctions:
    @pytest.mark.parametrize(
        "factory,expected_type",
        [
            (api_key_ref, "api_key"),
            (basic_ref, "basic"),
            (bearer_token_ref, "bearer_token"),
            (oauth_client_ref, "oauth_client"),
            (certificate_ref, "certificate"),
            (git_ssh_ref, "git_ssh"),
            (git_token_ref, "git_token"),
            (atlan_api_token_ref, "atlan_api_token"),
            (atlan_oauth_client_ref, "atlan_oauth_client"),
        ],
    )
    def test_factory_sets_credential_type(self, factory, expected_type):
        ref = factory("my-cred")
        assert ref.credential_type == expected_type
        assert ref.name == "my-cred"
        assert ref.store_name == "default"
        assert ref.credential_guid == ""

    def test_factory_accepts_store_name(self):
        ref = api_key_ref("my-cred", store_name="vault")
        assert ref.store_name == "vault"

    def test_legacy_credential_ref(self):
        ref = legacy_credential_ref("abc-123")
        assert ref.name == "abc-123"
        assert ref.credential_guid == "abc-123"
        assert ref.credential_type == "unknown"

    def test_legacy_credential_ref_with_type(self):
        ref = legacy_credential_ref("abc-123", credential_type="api_key")
        assert ref.credential_type == "api_key"
        assert ref.credential_guid == "abc-123"


class TestTemporalSerialization:
    """CredentialRef must survive Temporal's JSON serialization roundtrip."""

    def test_roundtrip_via_dict(self):
        import json

        ref = CredentialRef(
            name="prod-key",
            credential_type="api_key",
            store_name="vault",
            credential_guid="",
        )
        # Simulate Temporal's JSON serialization
        serialized = json.dumps(ref.model_dump())
        data = json.loads(serialized)
        restored = CredentialRef(**data)
        assert restored == ref

    def test_legacy_ref_roundtrip(self):
        import json

        ref = legacy_credential_ref("abc-123", "basic")
        serialized = json.dumps(ref.model_dump())
        data = json.loads(serialized)
        restored = CredentialRef(**data)
        assert restored == ref
        assert restored.credential_guid == "abc-123"


class TestCredentialResolvableProtocol:
    def test_extraction_input_satisfies_protocol(self):
        inp = ExtractionInput(credential_guid="abc-123")
        assert isinstance(inp, CredentialResolvable)

    def test_plain_object_does_not_satisfy(self):
        assert not isinstance("not-an-input", CredentialResolvable)


class TestCredentialRefResolve:
    def test_resolve_agent_with_populated_spec(self):
        spec = AgentCredentialSpec.model_validate(
            {
                "agent-name": "cloudsql-postgres-agent",
                "secret-manager": "awssecretmanager",
                "secret-path": "atlan/dev/test",
                "auth-type": "basic",
                "basic.username": "username",
                "basic.password": "password",
            }
        )
        inp = ExtractionInput(
            extraction_method="agent",
            agent_json=spec,
        )
        ref = CredentialRef.resolve(inp)
        assert ref.agent_spec is not None
        assert ref.credential_guid == ""

    def test_resolve_direct_with_guid(self):
        inp = ExtractionInput(
            extraction_method="direct",
            credential_guid="abc-123",
        )
        ref = CredentialRef.resolve(inp)
        assert ref.credential_guid == "abc-123"
        assert ref.agent_spec is None

    def test_resolve_empty_method_defaults_to_direct(self):
        inp = ExtractionInput(
            extraction_method="",
            credential_guid="abc-123",
        )
        ref = CredentialRef.resolve(inp)
        assert ref.credential_guid == "abc-123"

    def test_resolve_no_credential_source_raises(self):
        inp = ExtractionInput(
            extraction_method="direct",
            credential_guid="",
        )
        with pytest.raises(CredentialRoutingError):
            CredentialRef.resolve(inp)

    def test_resolve_non_resolvable_raises(self):
        with pytest.raises(CredentialResolvableTypeError):
            CredentialRef.resolve("not-a-model")  # type: ignore[arg-type]

    def test_resolve_agent_with_empty_spec_and_guid_falls_through(self):
        """Agent mode with unpopulated agent_json but valid guid should fail
        because resolve() requires explicit direct mode for GUID resolution."""
        inp = ExtractionInput(
            extraction_method="agent",
            credential_guid="abc-123",
        )
        with pytest.raises(CredentialRoutingError):
            CredentialRef.resolve(inp)

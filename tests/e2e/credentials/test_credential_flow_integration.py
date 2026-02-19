"""Integration tests for end-to-end credential flow.

Tests cover:
1. Credential declaration -> schema generation -> protocol resolution
2. Handler registration and configmap generation
3. CredentialHandle lifecycle (creation, access, audit logging)
4. Full workflow simulation with credential injection

These tests ensure all components work together correctly.
"""

from unittest.mock import patch

import pytest

from application_sdk.credentials import AuthMode, Credential, FieldSpec
from application_sdk.credentials.handle import CredentialHandle
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.types import ProtocolType


class TestCredentialDeclarationToSchemaFlow:
    """Test the flow from Credential declaration to schema generation."""

    def test_simple_api_key_declaration_generates_valid_schema(self):
        """Simple API key declaration should generate valid schema."""
        cred = Credential(
            name="stripe",
            auth=AuthMode.API_KEY,
            description="Stripe API credentials",
        )

        schema = CredentialResolver.get_credential_schema(cred)

        assert schema["name"] == "stripe"
        assert schema["auth_mode"] == "api_key"
        assert schema["protocol_type"] == "static_secret"
        assert schema["description"] == "Stripe API credentials"
        assert schema["required"] is True

        # Should have api_key field
        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names

        # api_key should be sensitive
        api_key_field = next(f for f in schema["fields"] if f["name"] == "api_key")
        assert api_key_field["sensitive"] is True

    def test_oauth_declaration_generates_all_required_fields(self):
        """OAuth declaration should generate all required fields."""
        cred = Credential(
            name="google",
            auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS,
        )

        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "client_id" in field_names
        assert "client_secret" in field_names

        # client_secret should be sensitive
        secret_field = next(f for f in schema["fields"] if f["name"] == "client_secret")
        assert secret_field["sensitive"] is True

    def test_extra_fields_are_included_in_schema(self):
        """Extra fields should be included in generated schema."""
        cred = Credential(
            name="snowflake",
            auth=AuthMode.DATABASE,
            extra_fields=[
                FieldSpec(name="account", display_name="Account Identifier"),
                FieldSpec(name="warehouse", display_name="Warehouse"),
                FieldSpec(name="role", default_value="PUBLIC"),
            ],
        )

        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "account" in field_names
        assert "warehouse" in field_names
        assert "role" in field_names

        # Check display names
        account_field = next(f for f in schema["fields"] if f["name"] == "account")
        assert account_field["display_name"] == "Account Identifier"

        # Check default value
        role_field = next(f for f in schema["fields"] if f["name"] == "role")
        assert role_field.get("default_value") == "PUBLIC"

    def test_field_overrides_customize_standard_fields(self):
        """Field overrides should customize standard protocol fields."""
        cred = Credential(
            name="custom_api",
            auth=AuthMode.API_KEY,
            fields={
                "api_key": FieldSpec(
                    name="api_key",
                    display_name="Custom API Secret",
                    placeholder="sk_xxx...",
                    help_text="Find in Dashboard -> API Keys",
                ),
            },
        )

        schema = CredentialResolver.get_credential_schema(cred)

        api_key_field = next(f for f in schema["fields"] if f["name"] == "api_key")
        assert api_key_field["display_name"] == "Custom API Secret"
        assert api_key_field["placeholder"] == "sk_xxx..."
        assert api_key_field["help_text"] == "Find in Dashboard -> API Keys"


class TestConfigmapGeneration:
    """Test configmap generation for UI widgets."""

    def test_configmap_has_correct_structure(self):
        """Configmap should have correct structure for UI consumption."""
        cred = Credential(
            name="api_cred",
            auth=AuthMode.API_KEY,
            description="API credentials",
        )

        configmap = CredentialResolver.get_configmap(cred)

        assert "id" in configmap
        assert "name" in configmap
        assert "description" in configmap
        assert "config" in configmap
        assert "properties" in configmap["config"]
        assert "steps" in configmap["config"]

    def test_configmap_properties_have_ui_metadata(self):
        """Configmap properties should have UI metadata."""
        cred = Credential(
            name="test",
            auth=AuthMode.BASIC_AUTH,
        )

        configmap = CredentialResolver.get_configmap(cred)
        properties = configmap["config"]["properties"]

        # Should have username and password fields
        assert "username" in properties
        assert "password" in properties

        # Password should use password widget
        assert properties["password"]["ui"]["widget"] == "password"

    def test_configmap_steps_list_all_properties(self):
        """Configmap steps should list all property names."""
        cred = Credential(
            name="test",
            auth=AuthMode.EMAIL_TOKEN,
            extra_fields=[
                FieldSpec(name="site_url"),
            ],
        )

        configmap = CredentialResolver.get_configmap(cred)
        steps = configmap["config"]["steps"]

        assert len(steps) >= 1
        step_properties = steps[0]["properties"]

        # All fields should be in step properties
        assert "email" in step_properties
        assert "api_token" in step_properties
        assert "site_url" in step_properties


class TestCredentialHandleIntegration:
    """Test CredentialHandle creation and access patterns."""

    def test_handle_provides_field_access(self):
        """CredentialHandle should provide field access via get()."""
        handle = CredentialHandle(
            slot_name="stripe",
            values={
                "api_key": "sk_test_xxx",
                "base_url": "https://api.stripe.com",
            },
            workflow_id="test-workflow",
            protocol_type="static_secret",
        )

        assert handle.get("api_key") == "sk_test_xxx"
        assert handle.get("base_url") == "https://api.stripe.com"

    def test_handle_returns_none_for_missing_fields(self):
        """CredentialHandle should return None for missing fields."""
        handle = CredentialHandle(
            slot_name="test",
            values={"api_key": "xxx"},
            workflow_id="test",
            protocol_type="static_secret",
        )

        assert handle.get("nonexistent") is None
        assert handle.get("missing", "default") == "default"

    def test_handle_logs_field_access(self):
        """CredentialHandle should log field access for audit."""
        handle = CredentialHandle(
            slot_name="stripe",
            values={"api_key": "xxx"},
            workflow_id="wf-123",
            protocol_type="static_secret",
        )

        with patch("application_sdk.credentials.handle.logger") as mock_logger:
            handle.get("api_key")

            # Should have logged the access
            mock_logger.debug.assert_called()
            call_kwargs = mock_logger.debug.call_args
            assert "api_key" in str(call_kwargs) or "extra" in call_kwargs.kwargs

    def test_handle_blocks_direct_iteration(self):
        """CredentialHandle should block direct iteration."""
        handle = CredentialHandle(
            slot_name="test",
            values={"api_key": "xxx", "secret": "yyy"},
            workflow_id="test",
            protocol_type="static_secret",
        )

        with pytest.raises(Exception):
            list(handle)

    def test_handle_blocks_dict_conversion(self):
        """CredentialHandle should block dict() conversion."""
        handle = CredentialHandle(
            slot_name="test",
            values={"api_key": "xxx"},
            workflow_id="test",
            protocol_type="static_secret",
        )

        with pytest.raises(Exception):
            dict(handle)


class TestProtocolResolutionFlow:
    """Test protocol resolution and application flow."""

    def test_resolve_to_apply_flow(self):
        """Full flow from Credential -> resolve -> apply."""
        # 1. Declare credential
        cred = Credential(
            name="stripe",
            auth=AuthMode.API_KEY,
        )

        # 2. Resolve to protocol
        protocol = CredentialResolver.resolve(cred)

        # 3. Apply to request
        result = protocol.apply({"api_key": "sk_test_xxx"}, {})

        # 4. Verify result
        assert result.headers == {"Authorization": "Bearer sk_test_xxx"}

    def test_resolve_with_config_override_flow(self):
        """Config override should affect protocol behavior."""
        cred = Credential(
            name="anthropic",
            auth=AuthMode.API_KEY,
            config_override={
                "header_name": "x-api-key",
                "prefix": "",
            },
        )

        protocol = CredentialResolver.resolve(cred)
        result = protocol.apply({"api_key": "sk-ant-xxx"}, {})

        assert result.headers == {"x-api-key": "sk-ant-xxx"}

    def test_direct_protocol_type_escape_hatch(self):
        """Direct ProtocolType specification should work."""
        cred = Credential(
            name="custom",
            protocol=ProtocolType.STATIC_SECRET,
            protocol_config={
                "location": "query",
                "param_name": "key",
            },
        )

        protocol = CredentialResolver.resolve(cred)
        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.query_params == {"key": "xxx"}


class TestMultiCredentialDeclaration:
    """Test apps declaring multiple credentials."""

    def test_multiple_credentials_generate_independent_schemas(self):
        """Multiple credentials should generate independent schemas."""
        creds = [
            Credential(name="stripe", auth=AuthMode.API_KEY),
            Credential(name="jira", auth=AuthMode.EMAIL_TOKEN),
            Credential(name="aws", auth=AuthMode.AWS_SIGV4),
        ]

        schemas = [CredentialResolver.get_credential_schema(c) for c in creds]

        assert schemas[0]["name"] == "stripe"
        assert schemas[0]["protocol_type"] == "static_secret"

        assert schemas[1]["name"] == "jira"
        assert schemas[1]["protocol_type"] == "identity_pair"

        assert schemas[2]["name"] == "aws"
        assert schemas[2]["protocol_type"] == "request_signing"

    def test_credentials_with_same_protocol_different_config(self):
        """Same protocol with different configs should work."""
        stripe = Credential(
            name="stripe",
            auth=AuthMode.API_KEY,
            # Default: Authorization: Bearer xxx
        )

        anthropic = Credential(
            name="anthropic",
            auth=AuthMode.API_KEY,
            config_override={"header_name": "x-api-key", "prefix": ""},
        )

        stripe_protocol = CredentialResolver.resolve(stripe)
        anthropic_protocol = CredentialResolver.resolve(anthropic)

        stripe_result = stripe_protocol.apply({"api_key": "sk_xxx"}, {})
        anthropic_result = anthropic_protocol.apply({"api_key": "ant_xxx"}, {})

        assert stripe_result.headers["Authorization"] == "Bearer sk_xxx"
        assert anthropic_result.headers["x-api-key"] == "ant_xxx"


class TestValidationFlow:
    """Test credential validation flow."""

    def test_validate_before_apply(self):
        """Validation should catch missing required fields."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        # Validate with missing api_key
        result = protocol.validate({})

        assert not result.valid
        assert len(result.errors) > 0

    def test_validation_passes_with_valid_credentials(self):
        """Validation should pass with all required fields."""
        cred = Credential(name="test", auth=AuthMode.BASIC_AUTH)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.validate(
            {
                "username": "user",
                "password": "pass",
            }
        )

        assert result.valid
        assert len(result.errors) == 0

    def test_validation_errors_are_descriptive(self):
        """Validation errors should be descriptive."""
        cred = Credential(name="test", auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.validate({})

        # Should mention what's missing
        error_text = " ".join(result.errors).lower()
        assert "client_id" in error_text or "client" in error_text

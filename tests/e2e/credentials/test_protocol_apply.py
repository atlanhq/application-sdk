"""Unit tests for protocol.apply() - verifies correct HTTP request construction.

These tests validate that each authentication pattern produces the exact headers,
query parameters, and body content that real APIs expect. This is the foundation
for proving the credential system works correctly.

Tests are organized by protocol type:
1. STATIC_SECRET - API keys in headers or query params
2. IDENTITY_PAIR - Basic Auth, Email+Token, Header pairs, Body credentials
3. TOKEN_EXCHANGE - OAuth2, JWT Bearer
4. REQUEST_SIGNING - AWS SigV4, HMAC
"""

import base64

from application_sdk.credentials import AuthMode, Credential, FieldSpec
from application_sdk.credentials.resolver import CredentialResolver


class TestStaticSecretProtocolApply:
    """Test STATIC_SECRET protocol produces correct request modifications."""

    def test_bearer_token_default(self):
        """Default API_KEY: Bearer token in Authorization header."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "sk_test_123"}, {})

        assert result.headers == {"Authorization": "Bearer sk_test_123"}
        assert result.query_params == {}
        assert result.body is None

    def test_anthropic_x_api_key_header(self):
        """Anthropic: x-api-key header without prefix.

        Docs: https://docs.anthropic.com/en/api/getting-started
        Required: x-api-key header with raw API key (no Bearer prefix)
        """
        cred = Credential(
            name="anthropic",
            auth=AuthMode.API_KEY,
            config_override={"header_name": "x-api-key", "prefix": ""},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "sk-ant-api03-xxx"}, {})

        assert result.headers == {"x-api-key": "sk-ant-api03-xxx"}

    def test_openai_authorization_bearer(self):
        """OpenAI: Standard Authorization Bearer.

        Docs: https://platform.openai.com/docs/api-reference/authentication
        Required: Authorization: Bearer YOUR-API-KEY
        """
        cred = Credential(name="openai", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "sk-proj-xxx"}, {})

        assert result.headers == {"Authorization": "Bearer sk-proj-xxx"}

    def test_cohere_bearer_token(self):
        """Cohere: Authorization BEARER format.

        Docs: https://docs.cohere.com/reference/about
        Required: Authorization: BEARER [API_KEY]
        Note: Cohere uses uppercase BEARER but HTTP headers are case-insensitive
        """
        cred = Credential(name="cohere", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.headers == {"Authorization": "Bearer xxx"}

    def test_github_token_prefix(self):
        """GitHub: token prefix instead of Bearer.

        Docs: https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api
        Required: Authorization: token YOUR-TOKEN or Authorization: Bearer YOUR-TOKEN
        """
        cred = Credential(
            name="github",
            auth=AuthMode.API_KEY,
            config_override={"prefix": "token "},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "ghp_xxx"}, {})

        assert result.headers == {"Authorization": "token ghp_xxx"}

    def test_sendgrid_bearer_token(self):
        """SendGrid: Standard Bearer token.

        Docs: https://docs.sendgrid.com/api-reference/how-to-use-the-sendgrid-v3-api/authentication
        Required: Authorization: Bearer SG.xxx
        """
        cred = Credential(name="sendgrid", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "SG.xxx"}, {})

        assert result.headers == {"Authorization": "Bearer SG.xxx"}

    def test_openweathermap_query_param(self):
        """OpenWeatherMap: API key in query parameter.

        Docs: https://openweathermap.org/appid
        Required: ?appid=YOUR_API_KEY
        """
        cred = Credential(
            name="openweather",
            auth=AuthMode.API_KEY_QUERY,
            config_override={"param_name": "appid"},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.headers == {}
        assert result.query_params == {"appid": "xxx"}

    def test_google_maps_query_param(self):
        """Google Maps: API key in query parameter.

        Docs: https://developers.google.com/maps/documentation/javascript/get-api-key
        Required: ?key=YOUR_API_KEY
        """
        cred = Credential(
            name="google_maps",
            auth=AuthMode.API_KEY_QUERY,
            config_override={"param_name": "key"},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "AIzaSyXXX"}, {})

        assert result.query_params == {"key": "AIzaSyXXX"}

    def test_newsapi_query_param(self):
        """NewsAPI: API key in query parameter.

        Docs: https://newsapi.org/docs/authentication
        Required: ?apiKey=YOUR_API_KEY
        """
        cred = Credential(
            name="newsapi",
            auth=AuthMode.API_KEY_QUERY,
            config_override={"param_name": "apiKey"},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.query_params == {"apiKey": "xxx"}

    def test_rapidapi_custom_header(self):
        """RapidAPI: Custom X-RapidAPI-Key header.

        Docs: https://docs.rapidapi.com/docs/keys
        Required: X-RapidAPI-Key: YOUR_KEY
        """
        cred = Credential(
            name="rapidapi",
            auth=AuthMode.API_KEY,
            config_override={"header_name": "X-RapidAPI-Key", "prefix": ""},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.headers == {"X-RapidAPI-Key": "xxx"}

    def test_huggingface_bearer(self):
        """Hugging Face: Bearer token.

        Docs: https://huggingface.co/docs/api-inference/index
        Required: Authorization: Bearer hf_xxx
        """
        cred = Credential(name="huggingface", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "hf_xxx"}, {})

        assert result.headers == {"Authorization": "Bearer hf_xxx"}

    def test_stability_ai_bearer(self):
        """Stability AI: Bearer token.

        Docs: https://platform.stability.ai/docs/api-reference
        Required: Authorization: Bearer sk-xxx
        """
        cred = Credential(name="stability", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "sk-xxx"}, {})

        assert result.headers == {"Authorization": "Bearer sk-xxx"}

    def test_replicate_bearer(self):
        """Replicate: Bearer token.

        Docs: https://replicate.com/docs/reference/http
        Required: Authorization: Bearer r8_xxx or Authorization: Token r8_xxx
        """
        cred = Credential(name="replicate", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "r8_xxx"}, {})

        assert result.headers == {"Authorization": "Bearer r8_xxx"}

    def test_mistral_bearer(self):
        """Mistral AI: Bearer token.

        Docs: https://docs.mistral.ai/api/
        Required: Authorization: Bearer xxx
        """
        cred = Credential(name="mistral", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.headers == {"Authorization": "Bearer xxx"}

    def test_groq_bearer(self):
        """Groq: Bearer token.

        Docs: https://console.groq.com/docs/quickstart
        Required: Authorization: Bearer gsk_xxx
        """
        cred = Credential(name="groq", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "gsk_xxx"}, {})

        assert result.headers == {"Authorization": "Bearer gsk_xxx"}

    def test_perplexity_bearer(self):
        """Perplexity: Bearer token.

        Docs: https://docs.perplexity.ai/reference/post_chat_completions
        Required: Authorization: Bearer pplx-xxx
        """
        cred = Credential(name="perplexity", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "pplx-xxx"}, {})

        assert result.headers == {"Authorization": "Bearer pplx-xxx"}

    def test_together_bearer(self):
        """Together AI: Bearer token.

        Docs: https://docs.together.ai/reference/authentication
        Required: Authorization: Bearer xxx
        """
        cred = Credential(name="together", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx"}, {})

        assert result.headers == {"Authorization": "Bearer xxx"}

    def test_fireworks_bearer(self):
        """Fireworks AI: Bearer token.

        Docs: https://docs.fireworks.ai/api-reference/authentication
        Required: Authorization: Bearer fw_xxx
        """
        cred = Credential(name="fireworks", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "fw_xxx"}, {})

        assert result.headers == {"Authorization": "Bearer fw_xxx"}

    def test_deepseek_bearer(self):
        """DeepSeek: Bearer token.

        Docs: https://platform.deepseek.com/api-docs
        Required: Authorization: Bearer sk-xxx
        """
        cred = Credential(name="deepseek", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "sk-xxx"}, {})

        assert result.headers == {"Authorization": "Bearer sk-xxx"}

    def test_field_alias_resolution(self):
        """Test that field aliases work (secret_key -> api_key)."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)
        protocol = CredentialResolver.resolve(cred)

        # Using "secret_key" alias instead of "api_key"
        result = protocol.apply({"secret_key": "xxx"}, {})

        assert result.headers == {"Authorization": "Bearer xxx"}


class TestIdentityPairProtocolApply:
    """Test IDENTITY_PAIR protocol produces correct request modifications."""

    def test_basic_auth_default(self):
        """Default Basic Auth: Base64 encoded username:password."""
        cred = Credential(name="test", auth=AuthMode.BASIC_AUTH)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"username": "user", "password": "pass"}, {})

        expected = base64.b64encode(b"user:pass").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_jira_email_token(self):
        """Jira/Atlassian: Email + API Token as Basic Auth.

        Docs: https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
        Required: Authorization: Basic base64(email:api_token)
        """
        cred = Credential(name="jira", auth=AuthMode.EMAIL_TOKEN)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"email": "user@example.com", "api_token": "xxx"}, {})

        expected = base64.b64encode(b"user@example.com:xxx").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_confluence_email_token(self):
        """Confluence: Same as Jira - Email + API Token."""
        cred = Credential(name="confluence", auth=AuthMode.EMAIL_TOKEN)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"email": "user@example.com", "api_token": "xxx"}, {})

        expected = base64.b64encode(b"user@example.com:xxx").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_bitbucket_email_app_password(self):
        """Bitbucket: Email + App Password as Basic Auth.

        Docs: https://support.atlassian.com/bitbucket-cloud/docs/app-passwords/
        Required: Authorization: Basic base64(email:app_password)
        """
        cred = Credential(
            name="bitbucket",
            auth=AuthMode.EMAIL_TOKEN,
            config_override={"secret_field": "app_password"},
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply(
            {"email": "user@example.com", "app_password": "xxx"}, {}
        )

        expected = base64.b64encode(b"user@example.com:xxx").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_plaid_header_pair(self):
        """Plaid: Two separate custom headers.

        Docs: https://plaid.com/docs/api/
        Required: PLAID-CLIENT-ID and PLAID-SECRET headers
        """
        cred = Credential(
            name="plaid",
            auth=AuthMode.HEADER_PAIR,
            config_override={
                "identity_field": "client_id",
                "secret_field": "secret",
                "identity_header": "PLAID-CLIENT-ID",
                "secret_header": "PLAID-SECRET",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"client_id": "xxx", "secret": "yyy"}, {})

        assert result.headers == {
            "PLAID-CLIENT-ID": "xxx",
            "PLAID-SECRET": "yyy",
        }

    def test_plaid_body_credentials(self):
        """Plaid: Credentials in request body (alternative).

        Docs: https://plaid.com/docs/api/
        Alternative: client_id and secret in JSON body
        """
        cred = Credential(
            name="plaid_body",
            auth=AuthMode.BODY_CREDENTIALS,
            config_override={
                "identity_field": "client_id",
                "secret_field": "secret",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"client_id": "xxx", "secret": "yyy"}, {})

        assert result.body == {"client_id": "xxx", "secret": "yyy"}
        assert result.headers == {}

    def test_twilio_account_sid_auth_token(self):
        """Twilio: Account SID + Auth Token as Basic Auth.

        Docs: https://www.twilio.com/docs/usage/api
        Required: Authorization: Basic base64(account_sid:auth_token)
        """
        cred = Credential(
            name="twilio",
            auth=AuthMode.BASIC_AUTH,
            config_override={
                "identity_field": "account_sid",
                "secret_field": "auth_token",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"account_sid": "ACxxx", "auth_token": "yyy"}, {})

        expected = base64.b64encode(b"ACxxx:yyy").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_stripe_basic_auth(self):
        """Stripe: API key as username in Basic Auth (primary method).

        Docs: https://stripe.com/docs/api/authentication
        Required: Authorization: Basic base64(api_key:) - empty password
        """
        cred = Credential(
            name="stripe_basic",
            auth=AuthMode.BASIC_AUTH,
            config_override={
                "identity_field": "api_key",
                "secret_field": "password",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        # Stripe uses API key as username with empty password
        result = protocol.apply({"api_key": "sk_test_xxx", "password": ""}, {})

        expected = base64.b64encode(b"sk_test_xxx:").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_mailchimp_api_key_basic(self):
        """Mailchimp: 'anystring' as username, API key as password.

        Docs: https://mailchimp.com/developer/marketing/guides/quick-start/
        Required: Authorization: Basic base64(anystring:api_key)
        """
        cred = Credential(
            name="mailchimp",
            auth=AuthMode.BASIC_AUTH,
            config_override={
                "identity_field": "username",
                "secret_field": "api_key",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"username": "anystring", "api_key": "xxx-us21"}, {})

        expected = base64.b64encode(b"anystring:xxx-us21").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_zendesk_email_token(self):
        """Zendesk: Email/token:api_token format.

        Docs: https://developer.zendesk.com/api-reference/introduction/security-and-auth/
        Required: Authorization: Basic base64(email/token:api_token)
        """
        cred = Credential(
            name="zendesk",
            auth=AuthMode.EMAIL_TOKEN,
        )
        protocol = CredentialResolver.resolve(cred)

        # Note: User needs to provide email as "user@example.com/token"
        result = protocol.apply(
            {"email": "user@example.com/token", "api_token": "xxx"}, {}
        )

        expected = base64.b64encode(b"user@example.com/token:xxx").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}

    def test_freshdesk_api_key_basic(self):
        """Freshdesk: API key as username, X as password.

        Docs: https://developers.freshdesk.com/api/#authentication
        Required: Authorization: Basic base64(api_key:X)
        """
        cred = Credential(
            name="freshdesk",
            auth=AuthMode.BASIC_AUTH,
            config_override={
                "identity_field": "api_key",
                "secret_field": "password",
            },
        )
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "xxx", "password": "X"}, {})

        expected = base64.b64encode(b"xxx:X").decode()
        assert result.headers == {"Authorization": f"Basic {expected}"}


class TestTokenExchangeProtocolApply:
    """Test TOKEN_EXCHANGE protocol (OAuth2) produces correct modifications."""

    def test_oauth2_bearer_token(self):
        """OAuth2: Access token as Bearer."""
        cred = Credential(name="test", auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS)
        protocol = CredentialResolver.resolve(cred)

        # After token exchange, we have an access_token
        result = protocol.apply({"access_token": "eyJxxx"}, {})

        assert result.headers == {"Authorization": "Bearer eyJxxx"}


class TestAWSSignatureProtocolApply:
    """Test AWS SigV4 signing produces correct modifications."""

    def test_aws_sigv4_fields(self):
        """AWS SigV4: Verify required fields are collected."""
        cred = Credential(
            name="aws",
            auth=AuthMode.AWS_SIGV4,
            config_override={"service": "s3", "region": "us-east-1"},
        )
        protocol = CredentialResolver.resolve(cred)

        # Verify the protocol has the right config
        assert protocol.config.get("service") == "s3"
        assert protocol.config.get("region") == "us-east-1"


class TestSchemaGenerationForRealAPIs:
    """Test that schema generation produces correct field sets for real APIs."""

    def test_anthropic_schema(self):
        """Anthropic schema has api_key field."""
        cred = Credential(
            name="anthropic",
            auth=AuthMode.API_KEY,
            config_override={"header_name": "x-api-key", "prefix": ""},
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "api_key" in field_names
        assert schema["auth_mode"] == "api_key"

    def test_jira_schema(self):
        """Jira schema has email and api_token fields."""
        cred = Credential(
            name="jira",
            auth=AuthMode.EMAIL_TOKEN,
            extra_fields=[FieldSpec(name="site_url")],
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "email" in field_names
        assert "api_token" in field_names
        assert "site_url" in field_names

    def test_plaid_header_pair_schema(self):
        """Plaid header pair schema has client_id and secret fields."""
        cred = Credential(
            name="plaid",
            auth=AuthMode.HEADER_PAIR,
            config_override={
                "identity_field": "client_id",
                "secret_field": "secret",
                "identity_header": "PLAID-CLIENT-ID",
                "secret_header": "PLAID-SECRET",
            },
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "client_id" in field_names
        assert "secret" in field_names

    def test_twilio_schema(self):
        """Twilio schema has account_sid and auth_token fields."""
        cred = Credential(
            name="twilio",
            auth=AuthMode.BASIC_AUTH,
            config_override={
                "identity_field": "account_sid",
                "secret_field": "auth_token",
            },
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "account_sid" in field_names
        assert "auth_token" in field_names

    def test_aws_schema(self):
        """AWS schema has access_key_id and secret_access_key fields."""
        cred = Credential(
            name="aws",
            auth=AuthMode.AWS_SIGV4,
            config_override={"service": "s3"},
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "access_key_id" in field_names
        assert "secret_access_key" in field_names

    def test_snowflake_schema(self):
        """Snowflake schema has all required connection fields."""
        cred = Credential(
            name="snowflake",
            auth=AuthMode.DATABASE,
            extra_fields=[
                FieldSpec(name="account"),
                FieldSpec(name="warehouse"),
                FieldSpec(name="database"),
                FieldSpec(name="schema"),
                FieldSpec(name="role"),
            ],
        )
        schema = CredentialResolver.get_credential_schema(cred)

        field_names = [f["name"] for f in schema["fields"]]
        assert "account" in field_names
        assert "warehouse" in field_names
        assert "database" in field_names

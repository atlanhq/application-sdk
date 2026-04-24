"""Tests for application_sdk.common.transforms.

Covers key format converters, dotted key expansion, auth section
flattening, and the full transform_agent_credentials pipeline.
"""

from application_sdk.common.transforms import (
    camel_to_kebab,
    expand_dotted_keys,
    flatten_auth_section,
    kebab_to_camel,
    transform_agent_credentials,
)


# -------------------------------------------------------------------------
# kebab_to_camel
# -------------------------------------------------------------------------


class TestKebabToCamel:
    def test_single_hyphen(self):
        assert kebab_to_camel("auth-type") == "authType"

    def test_multiple_hyphens(self):
        assert kebab_to_camel("aws-auth-method") == "awsAuthMethod"

    def test_no_hyphen_passthrough(self):
        assert kebab_to_camel("host") == "host"

    def test_empty_string(self):
        assert kebab_to_camel("") == ""

    def test_leading_hyphen(self):
        assert kebab_to_camel("-foo") == "Foo"

    def test_single_char_parts(self):
        assert kebab_to_camel("a-b-c") == "aBC"


# -------------------------------------------------------------------------
# camel_to_kebab
# -------------------------------------------------------------------------


class TestCamelToKebab:
    def test_single_capital(self):
        assert camel_to_kebab("authType") == "auth-type"

    def test_multiple_capitals(self):
        assert camel_to_kebab("awsAuthMethod") == "aws-auth-method"

    def test_no_capitals_passthrough(self):
        assert camel_to_kebab("host") == "host"

    def test_empty_string(self):
        assert camel_to_kebab("") == ""

    def test_roundtrip_kebab_to_camel_to_kebab(self):
        assert camel_to_kebab(kebab_to_camel("auth-type")) == "auth-type"
        assert camel_to_kebab(kebab_to_camel("aws-region")) == "aws-region"

    def test_roundtrip_camel_to_kebab_to_camel(self):
        assert kebab_to_camel(camel_to_kebab("authType")) == "authType"
        assert kebab_to_camel(camel_to_kebab("awsRegion")) == "awsRegion"


# -------------------------------------------------------------------------
# expand_dotted_keys
# -------------------------------------------------------------------------


class TestExpandDottedKeys:
    def test_empty_dict(self):
        assert expand_dotted_keys({}) == {}

    def test_no_dots_passthrough(self):
        assert expand_dotted_keys({"host": "h", "port": 5432}) == {
            "host": "h",
            "port": 5432,
        }

    def test_single_level_dot(self):
        assert expand_dotted_keys({"extra.database": "db"}) == {
            "extra": {"database": "db"}
        }

    def test_multiple_dotted_keys_same_root(self):
        result = expand_dotted_keys(
            {"basic.username": "u", "basic.password": "p"}
        )
        assert result == {"basic": {"username": "u", "password": "p"}}

    def test_mixed_dotted_and_plain(self):
        result = expand_dotted_keys(
            {"host": "h", "extra.database": "db", "port": 5432}
        )
        assert result == {"host": "h", "extra": {"database": "db"}, "port": 5432}

    def test_multi_level_dots(self):
        result = expand_dotted_keys({"a.b.c": 1})
        assert result == {"a": {"b": {"c": 1}}}

    def test_conflict_non_dict_root_wins(self):
        result = expand_dotted_keys({"extra": "just-a-string", "extra.db": "x"})
        assert result["extra"] == "just-a-string"

    def test_merge_with_existing_dict(self):
        result = expand_dotted_keys(
            {"extra": {"existing": "v"}, "extra.new_key": "v2"}
        )
        assert result["extra"]["existing"] == "v"
        assert result["extra"]["new_key"] == "v2"


# -------------------------------------------------------------------------
# flatten_auth_section
# -------------------------------------------------------------------------


class TestFlattenAuthSection:
    def test_basic_auth_flattening(self):
        creds = {
            "auth-type": "basic",
            "basic": {"username": "u", "password": "p"},
        }
        result = flatten_auth_section(creds)
        assert result["username"] == "u"
        assert result["password"] == "p"

    def test_deep_merge_extra(self):
        creds = {
            "auth-type": "gcp-wif",
            "extra": {"connect_type": "public"},
            "gcp-wif": {"extra": {"project_id": "p"}},
        }
        result = flatten_auth_section(creds)
        assert result["extra"]["connect_type"] == "public"
        assert result["extra"]["project_id"] == "p"

    def test_no_auth_type_passthrough(self):
        creds = {"host": "h", "port": 5432}
        result = flatten_auth_section(creds)
        assert result == {"host": "h", "port": 5432}

    def test_auth_type_with_no_matching_section(self):
        creds = {"auth-type": "basic", "host": "h"}
        result = flatten_auth_section(creds)
        assert result == {"auth-type": "basic", "host": "h"}

    def test_empty_auth_type_passthrough(self):
        creds = {"auth-type": "", "host": "h"}
        result = flatten_auth_section(creds)
        assert result == {"auth-type": "", "host": "h"}


# -------------------------------------------------------------------------
# transform_agent_credentials — full pipeline
# -------------------------------------------------------------------------


class TestTransformAgentCredentials:
    def test_auth_type_renamed(self):
        result = transform_agent_credentials({"auth-type": "basic"})
        assert result["authType"] == "basic"
        assert "auth-type" not in result

    def test_credential_source_added(self):
        result = transform_agent_credentials({"auth-type": "basic"})
        assert result["credentialSource"] == "agent"

    def test_extra_dot_expansion(self):
        result = transform_agent_credentials(
            {"auth-type": "basic", "extra.database": "db", "extra.ssl": True}
        )
        assert result["extra"] == {"database": "db", "ssl": True}
        assert "extra.database" not in result
        assert "extra.ssl" not in result

    def test_auth_prefix_field_promoted(self):
        result = transform_agent_credentials(
            {"auth-type": "basic", "basic.username": "u", "basic.password": "p"}
        )
        assert result["username"] == "u"
        assert result["password"] == "p"
        assert "basic.username" not in result
        assert "basic.password" not in result

    def test_auth_prefix_extra_overrides_root_extra(self):
        result = transform_agent_credentials(
            {
                "auth-type": "basic",
                "extra.database": "root_db",
                "basic.extra.database": "auth_db",
            }
        )
        assert result["extra"]["database"] == "auth_db"

    def test_non_dotted_keys_preserved(self):
        result = transform_agent_credentials(
            {
                "auth-type": "basic",
                "host": "h",
                "port": 5432,
                "agent-name": "test",
            }
        )
        assert result["host"] == "h"
        assert result["port"] == 5432
        assert result["agent-name"] == "test"

    def test_full_cloudsql_payload(self):
        """End-to-end: matches marketplace script output for a real payload."""
        payload = {
            "agent-name": "cloudsql-postgres-agent",
            "agent-type": "new-app-framework",
            "auth-type": "basic",
            "aws-auth-method": "iam",
            "azure-auth-method": "managed_identity",
            "basic.password": "password",
            "basic.username": "username",
            "connectBy": "host",
            "extra.compiled_url": "postgresql+asyncpg://host:5432/dev",
            "extra.database": "dev",
            "host": "host",
            "key-type": "single-key",
            "port": 5432,
        }
        result = transform_agent_credentials(payload)

        # auth-type → authType
        assert result["authType"] == "basic"
        assert "auth-type" not in result

        # credentialSource added
        assert result["credentialSource"] == "agent"

        # dotted keys expanded
        assert result["extra"] == {
            "compiled_url": "postgresql+asyncpg://host:5432/dev",
            "database": "dev",
        }
        assert result["username"] == "username"
        assert result["password"] == "password"

        # plain keys preserved
        assert result["host"] == "host"
        assert result["port"] == 5432
        assert result["agent-name"] == "cloudsql-postgres-agent"
        assert result["key-type"] == "single-key"

    def test_wif_payload(self):
        """AlloyDB WIF payload — auth prefix with nested extra."""
        payload = {
            "agent-name": "alloydb-agent",
            "auth-type": "workload_identity_federation",
            "extra.database": "qa_test_db",
            "workload_identity_federation.extra.__name": "projects/dev/instances/primary",
            "workload_identity_federation.username": "gsa@project.iam",
        }
        result = transform_agent_credentials(payload)

        assert result["authType"] == "workload_identity_federation"
        assert result["username"] == "gsa@project.iam"
        assert result["extra"]["database"] == "qa_test_db"
        assert result["extra"]["__name"] == "projects/dev/instances/primary"

    def test_no_auth_type_still_works(self):
        result = transform_agent_credentials({"host": "h", "port": 5432})
        assert result["host"] == "h"
        assert result["credentialSource"] == "agent"
        assert "authType" not in result

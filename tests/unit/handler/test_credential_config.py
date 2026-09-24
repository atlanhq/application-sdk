"""Tests for the credential ``config.json`` write guard.

The failure being prevented: a ``POST /workflows/v1/config/{guid}?type=credentials``
carrying a GUID but an effectively-empty body replaces a complete credential
record with ``{"credentialSource": "direct"}``. That destroys ``authType`` /
``host`` / ``port``, which have no copy in Vault, while everything the auth
branch consumes (``password``, ``extra``) survives — so the corruption is
invisible for service-account connections (the app's ``.get()`` default lands on
the right branch by accident) and fatal for Workload Identity Federation.

The schemas below are trimmed from real generated credential configmaps
(``app/generated/*/atlan-connectors-*.json``) and keep the three shapes that
matter:

* **BigQuery** — ``host``/``port`` are ``type: "conditional"``, required only
  when ``extra.connect_type == "private"``; ``extra.connect_type`` is required;
  auth branches are ``basic`` / ``gcp-wif``.
* **Snowflake** — ``host``/``port`` unconditionally required, five auth
  branches. Proves the guard needs no per-connector code.
* **Databricks** — same unconditional shape, three auth branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest

from application_sdk.handler.credential_config import (
    dropped_required_fields,
    load_credential_schema,
    repair_dropped_fields,
)

# ---------------------------------------------------------------------------
# Schemas (trimmed from the real generated configmaps)
# ---------------------------------------------------------------------------

_BIGQUERY_SCHEMA: dict[str, Any] = {
    "properties": {
        "name": {"type": "string", "required": False},
        "connector": {"type": "string", "required": False},
        "connectorType": {"type": "string", "required": False},
        "extra": {
            "type": "object",
            "properties": {
                "connect_type": {
                    "type": "string",
                    "required": True,
                    "enum": ["public", "private"],
                    "default": "public",
                }
            },
        },
        "host": {
            "type": "conditional",
            "required": False,
            "default": "https://bigquery.googleapis.com",
            "conditions": [
                {
                    "property": "extra.connect_type",
                    "value": "private",
                    "required": True,
                }
            ],
        },
        "port": {
            "type": "conditional",
            "required": False,
            "default": 443,
            "conditions": [
                {
                    "property": "extra.connect_type",
                    "value": "private",
                    "required": False,
                }
            ],
        },
        "auth-type": {
            "type": "string",
            "enum": ["basic", "gcp-wif"],
            "default": "basic",
            "required": True,
        },
        # The secret containers. `basic` holds username + password; `gcp-wif`
        # holds atlan_oauth_secret. Heracles strips both before this endpoint.
        "basic": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "required": True},
                "password": {"type": "string", "required": True},
                "extra": {"type": "object"},
            },
        },
        "gcp-wif": {"type": "object", "properties": {"extra": {"type": "object"}}},
    },
    "anyOf": [
        {"properties": {"auth-type": {"const": "basic"}}, "required": ["basic"]},
        {"properties": {"auth-type": {"const": "gcp-wif"}}, "required": ["gcp-wif"]},
        {
            "properties": {"extra.connect_type": {"const": "private"}},
            "required": ["host"],
        },
    ],
}

_SNOWFLAKE_SCHEMA: dict[str, Any] = {
    "properties": {
        "name": {"type": "string", "required": False},
        "host": {"type": "string", "required": True},
        "port": {"type": "number", "required": True},
        "auth-type": {"type": "string", "required": True},
        "basic": {"type": "object", "properties": {"password": {"required": True}}},
        "keypair": {"type": "object"},
        "okta": {"type": "object"},
        "custom_oauth": {"type": "object"},
        "entra_id": {"type": "object"},
    },
    "anyOf": [
        {"properties": {"auth-type": {"const": "basic"}}, "required": ["basic"]},
        {"properties": {"auth-type": {"const": "keypair"}}, "required": ["keypair"]},
        {"properties": {"auth-type": {"const": "okta"}}, "required": ["okta"]},
        {"properties": {"auth-type": {"const": "entra_id"}}, "required": ["entra_id"]},
        {
            "properties": {"auth-type": {"const": "custom_oauth"}},
            "required": ["custom_oauth"],
        },
    ],
}

_DATABRICKS_SCHEMA: dict[str, Any] = {
    "properties": {
        "host": {"type": "string", "required": True},
        "port": {"type": "number", "required": True},
        "auth-type": {"type": "string", "required": True},
        "basic": {"type": "object"},
        "aws_service": {"type": "object"},
        "azure_service": {"type": "object"},
    },
    "anyOf": [
        {"properties": {"auth-type": {"const": "basic"}}, "required": ["basic"]},
        {
            "properties": {"auth-type": {"const": "aws_service"}},
            "required": ["aws_service"],
        },
        {
            "properties": {"auth-type": {"const": "azure_service"}},
            "required": ["azure_service"],
        },
    ],
}

# ---------------------------------------------------------------------------
# Stored records, as they exist in the object store post-strip
# ---------------------------------------------------------------------------

#: A healthy BigQuery WIF record on a public endpoint. Note `authType` camelCase
#: while the schema declares `auth-type`, and the runtime alias `gcp-wif`.
_BQ_WIF_PUBLIC = {
    "name": "bq-wif",
    "connectorConfigName": "atlan-connectors-bigquery",
    "connectorType": "bigquery",
    "authType": "gcp-wif",
    "host": "https://bigquery.googleapis.com",
    "port": 443,
    "extra": {"connect_type": "public", "project_id": "example-project"},
    "credentialSource": "direct",
}

#: A healthy BigQuery service-account record behind Private Service Connect.
_BQ_SA_PRIVATE = {
    "name": "bq-psc",
    "connectorConfigName": "atlan-connectors-bigquery",
    "authType": "service_account",  # runtime alias, absent from the schema enum
    "host": "https://bigquery-private.p.googleapis.com",
    "port": 443,
    "extra": {"connect_type": "private", "project_id": "example-project"},
    "credentialSource": "direct",
}

#: The corruption signature — what a GUID-only request writes today.
_STUB_BODY = {"credentialSource": "direct"}


class TestDroppedRequiredFields:
    """The transition check itself."""

    def test_stub_body_over_wif_record_is_caught(self) -> None:
        """CONNECT-843: the body that silently broke IKEA's miner."""
        dropped = dropped_required_fields(_STUB_BODY, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA)
        assert "auth-type" in dropped

    def test_stub_body_over_private_record_also_loses_host(self) -> None:
        """A private connection additionally loses the endpoint.

        This is the worse half: an empty ``host`` resolves to ``None`` in
        ``resolve_bigquery_base``, so the client silently falls back to the
        public endpoint instead of failing.
        """
        dropped = dropped_required_fields(_STUB_BODY, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA)
        assert "auth-type" in dropped
        assert "host" in dropped

    def test_public_record_does_not_require_host(self) -> None:
        """``host`` is conditional — a public connection never demands it."""
        existing = {**_BQ_WIF_PUBLIC}
        existing.pop("host")
        dropped = dropped_required_fields(_STUB_BODY, existing, _BIGQUERY_SCHEMA)
        assert "host" not in dropped

    def test_private_to_public_switch_may_drop_host(self) -> None:
        """The regression a blind merge would cause.

        Switching back to the public endpoint legitimately omits ``host``. A
        ``{**existing, **body}`` merge would resurrect the stale private host and
        keep routing at a Private Service Connect endpoint the customer just
        turned off. Because the requirement is evaluated conditionally against
        the *incoming* body, nothing is flagged.
        """
        body = {
            "name": "bq-psc",
            "connectorConfigName": "atlan-connectors-bigquery",
            "authType": "service_account",
            "port": 443,
            "extra": {"connect_type": "public", "project_id": "example-project"},
        }
        assert dropped_required_fields(body, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA) == []

    def test_public_to_private_switch_without_host_is_caught(self) -> None:
        """Turning private on without a host is invalid in the other direction."""
        body = {
            "connectorConfigName": "atlan-connectors-bigquery",
            "authType": "service_account",
            "extra": {"connect_type": "private"},
        }
        assert "host" in dropped_required_fields(body, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA)

    def test_first_ever_create_is_never_blocked(self) -> None:
        """No stored object ⇒ nothing can be dropped."""
        assert dropped_required_fields(_STUB_BODY, None, _BIGQUERY_SCHEMA) == []
        assert dropped_required_fields(_STUB_BODY, {}, _BIGQUERY_SCHEMA) == []

    def test_no_schema_is_a_no_op(self) -> None:
        """An app without a generated credential configmap is unaffected."""
        assert dropped_required_fields(_STUB_BODY, _BQ_WIF_PUBLIC, None) == []

    def test_complete_body_passes_untouched(self) -> None:
        assert (
            dropped_required_fields(_BQ_WIF_PUBLIC, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA)
            == []
        )

    def test_auth_type_value_change_is_allowed(self) -> None:
        """Changing the auth type is an edit, not a loss."""
        body = {**_BQ_WIF_PUBLIC, "authType": "basic"}
        assert dropped_required_fields(body, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA) == []

    def test_blank_value_counts_as_dropped(self) -> None:
        """``authType: ""`` selects no auth branch, so it is a loss."""
        body = {**_BQ_WIF_PUBLIC, "authType": ""}
        assert "auth-type" in dropped_required_fields(
            body, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA
        )

    def test_null_host_on_private_counts_as_dropped(self) -> None:
        body = {**_BQ_SA_PRIVATE, "host": None}
        assert "host" in dropped_required_fields(body, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA)

    def test_kebab_case_record_matches_kebab_schema(self) -> None:
        """A producer sending ``auth-type`` verbatim is understood too."""
        existing = {k: v for k, v in _BQ_WIF_PUBLIC.items() if k != "authType"}
        existing["auth-type"] = "gcp-wif"
        assert "auth-type" in dropped_required_fields(
            _STUB_BODY, existing, _BIGQUERY_SCHEMA
        )

    def test_nested_extra_requirement_is_checked(self) -> None:
        """``extra.connect_type`` is required and lives in the redacted copy."""
        assert "extra.connect_type" in dropped_required_fields(
            _STUB_BODY, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA
        )


class TestSecretsAreNeverRequired:
    """ "Only skipping secrets/password" — the exclusion rules."""

    def test_auth_branch_objects_are_not_required(self) -> None:
        """``basic`` / ``gcp-wif`` hold the secrets and never reach this endpoint.

        Demanding them would reject every legitimate write, since Heracles strips
        them by design.
        """
        dropped = dropped_required_fields(_STUB_BODY, _BQ_WIF_PUBLIC, _BIGQUERY_SCHEMA)
        assert "basic" not in dropped
        assert "gcp-wif" not in dropped

    def test_username_and_password_are_not_required(self) -> None:
        existing = {**_BQ_SA_PRIVATE, "username": "sa@example.iam.gserviceaccount.com"}
        dropped = dropped_required_fields(_STUB_BODY, existing, _BIGQUERY_SCHEMA)
        assert "username" not in dropped
        assert "password" not in dropped

    @pytest.mark.parametrize(
        "secret_field",
        [
            "atlan_oauth_secret",
            "client_secret",
            "private_key",
            "private-key",
            "passphrase",
            "some_password",
            "access_token",
        ],
    )
    def test_secret_named_extra_children_are_not_required(
        self, secret_field: str
    ) -> None:
        """Mirrors Heracles' ``extra`` redaction list."""
        schema = {
            "properties": {
                "extra": {
                    "type": "object",
                    "properties": {secret_field: {"required": True}},
                }
            }
        }
        existing = {
            "connectorConfigName": "x",
            "extra": {secret_field: "redacted-in-store"},
        }
        assert dropped_required_fields(_STUB_BODY, existing, schema) == []


class TestGeneralisesAcrossConnectors:
    """One SDK change, every connector — no per-connector branching."""

    def test_snowflake_stub_body_loses_host_port_and_auth_type(self) -> None:
        existing = {
            "connectorConfigName": "atlan-connectors-snowflake",
            "authType": "basic",
            "host": "acme.snowflakecomputing.com",
            "port": 443,
        }
        dropped = dropped_required_fields(_STUB_BODY, existing, _SNOWFLAKE_SCHEMA)
        assert set(dropped) == {"host", "port", "auth-type"}

    def test_databricks_stub_body_loses_host_port_and_auth_type(self) -> None:
        existing = {
            "connectorConfigName": "atlan-connectors-databricks",
            "authType": "aws_service",
            "host": "dbc-example.cloud.databricks.com",
            "port": 443,
        }
        dropped = dropped_required_fields(_STUB_BODY, existing, _DATABRICKS_SCHEMA)
        assert set(dropped) == {"host", "port", "auth-type"}

    def test_snowflake_auth_branches_are_all_excluded(self) -> None:
        """Five auth types, all secret containers, none required."""
        existing = {
            "connectorConfigName": "atlan-connectors-snowflake",
            "authType": "keypair",
            "host": "acme.snowflakecomputing.com",
            "port": 443,
            "keypair": {"password": "x"},
        }
        dropped = dropped_required_fields(_STUB_BODY, existing, _SNOWFLAKE_SCHEMA)
        for branch in ("basic", "keypair", "okta", "custom_oauth", "entra_id"):
            assert branch not in dropped


class TestRepairDroppedFields:
    """Repair restores exactly what went missing — not a blind merge."""

    def test_restores_dropped_fields_only(self) -> None:
        dropped = dropped_required_fields(_STUB_BODY, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA)
        repaired = repair_dropped_fields(_STUB_BODY, _BQ_SA_PRIVATE, dropped)

        assert repaired["authType"] == "service_account"
        assert repaired["host"] == "https://bigquery-private.p.googleapis.com"
        assert repaired["extra"]["connect_type"] == "private"
        # Not resurrected: `name` is not required, so it stays dropped.
        assert "name" not in repaired

    def test_repaired_body_passes_a_second_pass(self) -> None:
        """Idempotence: re-running the guard on the repaired body finds nothing."""
        dropped = dropped_required_fields(_STUB_BODY, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA)
        repaired = repair_dropped_fields(_STUB_BODY, _BQ_SA_PRIVATE, dropped)
        assert dropped_required_fields(repaired, _BQ_SA_PRIVATE, _BIGQUERY_SCHEMA) == []

    def test_does_not_mutate_the_incoming_body(self) -> None:
        body = {"credentialSource": "direct"}
        repair_dropped_fields(body, _BQ_SA_PRIVATE, ["host", "auth-type"])
        assert body == {"credentialSource": "direct"}

    def test_preserves_the_incoming_camel_spelling(self) -> None:
        """A body that already uses ``authType`` keeps it, gaining no duplicate."""
        body = {"authType": "", "credentialSource": "direct"}
        repaired = repair_dropped_fields(body, _BQ_WIF_PUBLIC, ["auth-type"])
        assert repaired["authType"] == "gcp-wif"
        assert "auth-type" not in repaired


class TestLoadCredentialSchema:
    """Schema discovery via ``connectorConfigName``."""

    @staticmethod
    def _write(tmp_path: Path, stem: str, payload: dict[str, Any]) -> Path:
        generated = tmp_path / "generated" / "crawler"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / f"{stem}.json").write_bytes(orjson.dumps(payload))
        return tmp_path / "generated"

    def test_found_via_incoming_body(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path, "atlan-connectors-bigquery", {"config": _BIGQUERY_SCHEMA}
        )
        schema = load_credential_schema(
            {"connectorConfigName": "atlan-connectors-bigquery"}, None, root
        )
        assert schema is not None
        assert "auth-type" in schema["properties"]

    def test_found_via_stored_record_when_body_is_a_stub(self, tmp_path: Path) -> None:
        """The stub body names no connector — the stored copy must identify it.

        Without this fallback the guard could never fire on the exact body it
        exists to catch.
        """
        root = self._write(
            tmp_path, "atlan-connectors-bigquery", {"config": _BIGQUERY_SCHEMA}
        )
        schema = load_credential_schema(_STUB_BODY, _BQ_WIF_PUBLIC, root)
        assert schema is not None

    def test_missing_connector_config_name_yields_none(self, tmp_path: Path) -> None:
        root = self._write(tmp_path, "atlan-connectors-bigquery", {"config": {}})
        assert load_credential_schema(_STUB_BODY, {}, root) is None

    def test_unknown_connector_config_name_yields_none(self, tmp_path: Path) -> None:
        root = self._write(
            tmp_path, "atlan-connectors-bigquery", {"config": _BIGQUERY_SCHEMA}
        )
        assert (
            load_credential_schema({"connectorConfigName": "nope"}, None, root) is None
        )

    def test_missing_generated_dir_yields_none(self, tmp_path: Path) -> None:
        assert (
            load_credential_schema(
                {"connectorConfigName": "atlan-connectors-bigquery"},
                None,
                tmp_path / "absent",
            )
            is None
        )

    def test_unreadable_configmap_degrades_to_none(self, tmp_path: Path) -> None:
        """A malformed configmap must not make credentials unsavable."""
        generated = tmp_path / "generated" / "crawler"
        generated.mkdir(parents=True)
        (generated / "atlan-connectors-bigquery.json").write_bytes(b"{not json")
        assert (
            load_credential_schema(
                {"connectorConfigName": "atlan-connectors-bigquery"},
                None,
                tmp_path / "generated",
            )
            is None
        )

    def test_configmap_without_config_block_yields_none(self, tmp_path: Path) -> None:
        root = self._write(tmp_path, "atlan-connectors-bigquery", {"icon": "x"})
        assert (
            load_credential_schema(
                {"connectorConfigName": "atlan-connectors-bigquery"}, None, root
            )
            is None
        )

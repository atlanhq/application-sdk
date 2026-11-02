"""Required credential fields read from generated forms (FND-502).

The fixtures are BigQuery's real generated forms, trimmed to their schema
structure (UI copy removed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.requirements import (
    agent_schema,
    canonical_credential,
    credential_form,
    credential_from_pairs,
    credential_slots,
    load_form,
    missing_required_fields,
)

_GENERATED = (
    Path(__file__).parents[1]
    / "handler"
    / "fixtures"
    / "generated_bigquery_credentials"
)


@pytest.fixture(scope="module")
def form() -> dict[str, Any]:
    loaded = credential_form(_GENERATED, "atlan-connectors-bigquery")
    assert loaded is not None
    return dict(loaded)


@pytest.fixture(scope="module")
def workflow_form() -> dict[str, Any]:
    loaded = load_form(_GENERATED / "crawler" / "bigquery-crawler.json")
    assert loaded is not None
    return dict(loaded)


def _wif(**extra: str) -> dict[str, Any]:
    fields = {
        "project_id": "p",
        "service_account_email": "sa@example.invalid",
        "wif_pool_provider_id": "pool",
        "atlan_oauth_id": "client",
        "atlan_oauth_secret": "secret",
        "connect_type": "public",
    }
    fields.update(extra)
    return {"authType": "gcp-wif", "extra": {k: v for k, v in fields.items() if v}}


class TestCredentialForm:
    def test_gcp_wif_without_oauth_client_id_is_reported(self, form) -> None:
        """FND-502: the BigQuery WIF credential missing atlan_oauth_id."""
        assert missing_required_fields(form, _wif(atlan_oauth_id="")) == [
            "extra.atlan_oauth_id"
        ]

    def test_complete_gcp_wif_credential_passes(self, form) -> None:
        assert missing_required_fields(form, _wif()) == []

    def test_only_the_selected_auth_types_fields_are_required(self, form) -> None:
        """A WIF credential is not asked for basic's username/password."""
        gaps = missing_required_fields(form, _wif())
        assert "username" not in gaps and "password" not in gaps

    def test_basic_credential_reports_each_missing_field(self, form) -> None:
        credential = {"authType": "basic", "username": "u", "extra": {}}
        assert missing_required_fields(form, credential) == [
            "password",
            "extra.project_id",
        ]

    def test_auth_type_falls_back_to_the_forms_default(self, form) -> None:
        # The form defaults auth-type to "basic"; extra.connect_type defaults too.
        assert missing_required_fields(form, {"extra": {"project_id": "p"}}) == [
            "username",
            "password",
        ]

    def test_required_field_with_a_default_is_satisfied(self, form) -> None:
        """extra.connect_type is required but defaults to "public"."""
        credential = _wif(connect_type="")
        assert "extra.connect_type" not in missing_required_fields(form, credential)

    def test_blank_values_count_as_missing(self, form) -> None:
        credential = _wif()
        credential["extra"]["atlan_oauth_id"] = ""
        assert missing_required_fields(form, credential) == ["extra.atlan_oauth_id"]


class TestImplications:
    """``anyOf`` rules read as "when these consts match, these are required"."""

    _SCHEMA: dict[str, Any] = {
        "properties": {
            "host": {"type": "string", "required": False},
            "extra": {
                "type": "object",
                "properties": {"connect_type": {"type": "string", "default": "public"}},
            },
        },
        "anyOf": [
            {
                "properties": {"extra.connect_type": {"const": "private"}},
                "required": ["host"],
            }
        ],
    }

    def test_rule_applies_when_its_condition_holds(self) -> None:
        credential = {"extra": {"connect_type": "private"}}
        assert missing_required_fields(self._SCHEMA, credential) == ["host"]

    def test_rule_is_inert_when_its_condition_does_not_hold(self) -> None:
        assert missing_required_fields(self._SCHEMA, {}) == []

    def test_bigquery_private_link_host_has_a_default(self, form) -> None:
        """BigQuery's host requirement is met by its declared default."""
        assert missing_required_fields(form, _wif(connect_type="private")) == []


class TestAgentSchema:
    def test_agent_mapping_without_oauth_client_id_is_reported(
        self, workflow_form
    ) -> None:
        schema = agent_schema(workflow_form, "atlan-connectors-bigquery")
        assert schema is not None
        raw = {
            "auth-type": "gcp-wif",
            "gcp-wif.extra.project_id": "k1",
            "gcp-wif.extra.service_account_email": "k2",
            "gcp-wif.extra.wif_pool_provider_id": "k3",
            "gcp-wif.extra.atlan_oauth_secret": "k5",
        }
        assert missing_required_fields(schema, canonical_credential(raw)) == [
            "extra.atlan_oauth_id"
        ]

    def test_empty_default_does_not_satisfy_a_required_mapping(
        self, workflow_form
    ) -> None:
        """The agent schema's basic fields default to "": still required."""
        schema = agent_schema(workflow_form, "atlan-connectors-bigquery")
        assert schema is not None
        raw = {"auth-type": "basic", "basic.username": "k1"}
        assert missing_required_fields(schema, canonical_credential(raw)) == [
            "password"
        ]


class TestWorkflowForm:
    def test_credential_slot_names_its_type_and_requiredness(
        self, workflow_form
    ) -> None:
        slots = credential_slots(workflow_form)
        assert [(s.field, s.credential_type, s.required) for s in slots] == [
            ("credential-guid", "atlan-connectors-bigquery", True)
        ]

    def test_unknown_or_unsafe_credential_type_has_no_form(self) -> None:
        assert credential_form(_GENERATED, "atlan-connectors-nope") is None
        assert credential_form(_GENERATED, "../atlan-connectors-bigquery") is None
        assert credential_form(_GENERATED, "") is None


@pytest.mark.parametrize(
    "flat",
    [
        {"auth-type": "gcp-wif", "gcp-wif.extra.atlan_oauth_id": "k", "x": "1"},
        {"auth-type": "basic", "basic.username": "u", "extra.project_id": "p"},
        {
            "auth-type": "gcp-wif",
            "extra.project_id": "root",
            "gcp-wif.extra.project_id": "wins",
        },
        {"username": "u", "extra.region": "r"},
    ],
)
def test_canonical_credential_matches_the_runtime_transform(
    flat: dict[str, Any],
) -> None:
    """The api-side expansion agrees with ``transform_agent_credentials``."""
    runtime = transform_agent_credentials(flat)
    runtime.pop("credentialSource")
    runtime = {k: v for k, v in runtime.items() if "." not in k}
    assert canonical_credential(flat) == runtime


def test_v3_pairs_fold_into_the_canonical_shape() -> None:
    pairs = [
        {"key": "authType", "value": "gcp-wif"},
        {"key": "extra.atlan_oauth_id", "value": "client"},
        {"key": "extra.project_id", "value": "p"},
    ]
    assert credential_from_pairs(pairs) == {
        "authType": "gcp-wif",
        "extra": {"atlan_oauth_id": "client", "project_id": "p"},
    }


def test_fixture_is_the_real_generated_shape() -> None:
    """Guards the fixture against hand edits that drop its structure."""
    data = json.loads((_GENERATED / "atlan-connectors-bigquery.json").read_text())
    props = data["config"]["properties"]
    assert props["auth-type"]["enum"] == ["basic", "gcp-wif"]
    assert props["gcp-wif"]["properties"]["extra"]["properties"]["atlan_oauth_id"][
        "required"
    ]

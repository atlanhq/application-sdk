"""Tests for the ``agent_json`` ingress normaliser.

These are the behavioural contract for every reader of the field: the six
places that each used to tolerate the wire variety on their own now delegate
here, so this file is where the tolerance is pinned.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from application_sdk.credentials.ingress import (
    declared_agent_spec_type,
    lift_agent_json,
    normalize_agent_json,
)
from application_sdk.credentials.spec import AgentCredentialSpec

# The marketplace-package template default: every field name submitted as its
# own value, because a sibling Argo param indexes straight into the JSON and has
# to evaluate even in direct mode. ``port`` is the spec's only non-str field, so
# this coerces to a dict yet fails typed validation.
PLACEHOLDER = {
    "agent-name": "agent-name",
    "aws-auth-method": "aws-auth-method",
    "host": "host",
    "port": "port",
    "secret-manager": "secret-manager",
}

REAL = {
    "agent-name": "acme-agent",
    "secret-path": "arn:aws:secretsmanager:us-east-1:1:secret:x",
    "host": "db.example.com",
    "port": 1521,
}


class TestNormalizeAgentJson:
    """``normalize_agent_json``: one value → a typed spec, or ``None``."""

    def test_dict_becomes_a_spec(self) -> None:
        spec = normalize_agent_json(REAL)
        assert spec is not None
        assert spec.agent_name == "acme-agent"
        assert spec.port == 1521

    def test_json_string_becomes_a_spec(self) -> None:
        spec = normalize_agent_json(json.dumps(REAL))
        assert spec is not None
        assert spec.agent_name == "acme-agent"

    def test_existing_spec_passes_through_unchanged(self) -> None:
        original = AgentCredentialSpec.model_validate(REAL)
        assert normalize_agent_json(original) is original

    def test_connector_dotted_keys_survive(self) -> None:
        spec = normalize_agent_json({"agent-name": "acme", "basic.username": "u"})
        assert spec is not None
        assert spec.to_raw_dict()["basic.username"] == "u"

    def test_placeholder_is_absent(self) -> None:
        assert normalize_agent_json(PLACEHOLDER) is None

    def test_meaningless_values_are_absent(self) -> None:
        for value in (None, "", "  ", "{}", {}, "{", "not json", "[1, 2]", '"x"'):
            assert normalize_agent_json(value) is None, value

    def test_valid_but_unpopulated_spec_is_kept(self) -> None:
        """``is_populated()`` is the consumers' call, not ingress's. A name-only
        spec parses, so it is returned and left for the resolver to reject."""
        spec = normalize_agent_json({"agent-name": "acme"})
        assert spec is not None
        assert not spec.is_populated()

    def test_spec_type_narrows_validation(self) -> None:
        class StrictSpec(AgentCredentialSpec):
            model_config = ConfigDict(
                extra="forbid", frozen=True, populate_by_name=True
            )

        assert normalize_agent_json(REAL, spec_type=StrictSpec) is not None
        # ``basic.username`` is not declared on the subclass → forbidden.
        assert (
            normalize_agent_json(
                {"agent-name": "acme", "basic.username": "u"}, spec_type=StrictSpec
            )
            is None
        )

    def test_base_spec_is_revalidated_against_a_narrower_type(self) -> None:
        """A reader declaring a subclass gets the subclass, not the base — its
        own field would reject a base instance."""

        class StrictSpec(AgentCredentialSpec):
            model_config = ConfigDict(
                extra="forbid", frozen=True, populate_by_name=True
            )

        base = AgentCredentialSpec.model_validate(REAL)
        narrowed = normalize_agent_json(base, spec_type=StrictSpec)
        assert isinstance(narrowed, StrictSpec)
        assert narrowed.agent_name == "acme-agent"


class TestDeclaredAgentSpecType:
    """``declared_agent_spec_type``: read the spec class a model asks for."""

    def test_base_annotation(self) -> None:
        class _Input(BaseModel):
            agent_json: AgentCredentialSpec | None = None

        assert declared_agent_spec_type(_Input) is AgentCredentialSpec

    def test_subclass_annotation(self) -> None:
        class _Spec(AgentCredentialSpec):
            pass

        class _Input(BaseModel):
            agent_json: _Spec | None = None

        assert declared_agent_spec_type(_Input) is _Spec

    def test_missing_field_falls_back_to_the_base(self) -> None:
        class _Input(BaseModel):
            pass

        assert declared_agent_spec_type(_Input) is AgentCredentialSpec


class TestLiftAgentJson:
    """``lift_agent_json``: surface the freshest binding as a typed field.

    Heracles forwards the frontend payload verbatim, so the binding can arrive
    nested (metadata / connection_config / credentials) and in several competing
    copies at once — a live hyphen ``agent-json`` object next to a stale
    underscore ``agent_json`` serialized string. The lift must surface the
    freshest copy at the canonical top-level ``agent_json`` key, typed.
    """

    _FRESH = {"agent-name": "acme", "basic.username": "USERNAME", "host": "h"}
    _STALE = '{"agent-name": "acme", "basic.username": "username", "host": "h"}'

    @staticmethod
    def _raw(body: dict) -> dict:
        """The promoted spec back as a wire dict, for comparison."""
        return body["agent_json"].to_raw_dict()

    def test_promotes_a_typed_spec_not_a_dict(self) -> None:
        out = lift_agent_json({"metadata": {"agent-json": self._FRESH}})
        assert isinstance(out["agent_json"], AgentCredentialSpec)

    # ---- competing copies -------------------------------------------------

    def test_object_beats_string_and_hyphen_beats_underscore(self) -> None:
        # A parsed object is the live form state; a serialized string is a
        # snapshot that lags behind the user's edits.
        both = lift_agent_json(
            {"metadata": {"agent_json": self._STALE, "agent-json": self._FRESH}}
        )
        assert self._raw(both)["basic.username"] == "USERNAME"
        # ...and among two strings, the canonical hyphen spelling wins.
        strings = lift_agent_json(
            {
                "metadata": {
                    "agent_json": self._STALE,
                    "agent-json": json.dumps(self._FRESH),
                }
            }
        )
        assert self._raw(strings)["basic.username"] == "USERNAME"

    def test_sage_shape_picks_fresh_object_over_stale_string(self) -> None:
        # /sage: heracles forwards formData -> metadata carrying BOTH copies.
        body = {
            "credentials": {"connectorConfigName": "atlan-connectors-mssql"},
            "metadata": {
                "agent_json": self._STALE,
                "agent-json": self._FRESH,
                "extraction-method": "agent",
            },
        }
        out = lift_agent_json(body)
        assert self._raw(out)["basic.username"] == "USERNAME"
        # both agent-json keys stripped from the container
        assert "agent_json" not in out["metadata"]
        assert "agent-json" not in out["metadata"]
        assert out["metadata"]["extraction-method"] == "agent"

    def test_serialized_string_used_as_fallback_when_no_object(self) -> None:
        out = lift_agent_json({"metadata": {"agent_json": self._STALE}})
        assert self._raw(out)["basic.username"] == "username"

    def test_typed_spec_beats_a_stale_serialized_string(self) -> None:
        """A typed spec is the most-processed form — it must rank as parsed.

        Ranking it with the serialized strings would let a stale string
        snapshot (which lags the user's edits) beat a current typed spec —
        the stale-credential selection the freshness ordering exists to
        prevent.
        """
        typed = AgentCredentialSpec.model_validate(self._FRESH)
        out = lift_agent_json(
            {"metadata": {"agent_json": self._STALE, "agentJson": typed}}
        )
        assert self._raw(out)["basic.username"] == "USERNAME"

    def test_top_level_fresh_hyphen_overrides_stale_underscore(self) -> None:
        out = lift_agent_json({"agent_json": self._STALE, "agent-json": self._FRESH})
        assert self._raw(out)["basic.username"] == "USERNAME"
        # stale aliases are dropped; only the canonical top-level key remains
        assert "agent-json" not in out

    def test_top_level_wins_over_a_nested_copy_of_equal_freshness(self) -> None:
        out = lift_agent_json(
            {
                "agent-json": self._FRESH,
                "metadata": {"agent-json": {"agent-name": "other"}},
            }
        )
        assert out["agent_json"].agent_name == "acme"

    # ---- positions --------------------------------------------------------

    def test_agent_json_inside_credentials_dict(self) -> None:
        out = lift_agent_json(
            {"credentials": {"agent-json": self._FRESH, "connectorConfigName": "x"}}
        )
        assert out["agent_json"].agent_name == "acme"
        assert "agent-json" not in out["credentials"]
        assert out["credentials"]["connectorConfigName"] == "x"

    def test_agent_json_inside_v3_credentials_list(self) -> None:
        out = lift_agent_json(
            {
                "credentials": [
                    {"key": "agent-json", "value": self._FRESH},
                    {"key": "connectorConfigName", "value": "x"},
                ]
            }
        )
        assert out["agent_json"].agent_name == "acme"
        assert all(c["key"] != "agent-json" for c in out["credentials"])

    def test_agent_json_inside_connection_config(self) -> None:
        out = lift_agent_json({"connection_config": {"agentJson": self._FRESH}})
        assert out["agent_json"].agent_name == "acme"
        assert "agentJson" not in out["connection_config"]

    # ---- nothing to lift --------------------------------------------------

    def test_direct_mode_body_unchanged(self) -> None:
        body = {"credentials": {"host": "h", "username": "u"}}
        assert lift_agent_json(body) == body

    def test_empty_agent_json_ignored(self) -> None:
        assert "agent_json" not in lift_agent_json({"metadata": {"agent-json": {}}})
        assert "agent_json" not in lift_agent_json({"metadata": {"agent_json": ""}})

    def test_unparseable_string_ignored(self) -> None:
        assert "agent_json" not in lift_agent_json({"metadata": {"agent_json": "{"}})

    # ---- rendered-but-unfilled agent widget (placeholder values) ----------
    #
    # Promoting the placeholder made PreflightInput/AuthInput/MetadataInput raise
    # an unhandled ValidationError -> plain-text 500 -> opaque JSON-decode error
    # at the caller, on every direct-mode request whose form renders the widget.

    def test_placeholder_widget_is_not_promoted(self) -> None:
        out = lift_agent_json({"metadata": {"agent-json": PLACEHOLDER}})
        assert (
            "agent_json" not in out
        ), "a placeholder spec must not reach the typed agent_json field"

    def test_placeholder_inside_credentials_is_still_stripped(self) -> None:
        """Not promoted, but still removed — otherwise the v2→v3 credential shim
        flattens it into a bogus ``agent-json`` credential pair."""
        out = lift_agent_json({"credentials": {"agent-json": PLACEHOLDER, "host": "h"}})
        assert "agent_json" not in out
        assert "agent-json" not in out["credentials"]
        assert out["credentials"]["host"] == "h"

    def test_placeholder_in_v3_credentials_list_is_still_stripped(self) -> None:
        out = lift_agent_json(
            {
                "credentials": [
                    {"key": "agent-json", "value": PLACEHOLDER},
                    {"key": "host", "value": "h"},
                ]
            }
        )
        assert "agent_json" not in out
        assert all(c["key"] != "agent-json" for c in out["credentials"])

    def test_real_spec_is_still_promoted(self) -> None:
        out = lift_agent_json({"metadata": {"agent-json": REAL}})
        assert out["agent_json"] == AgentCredentialSpec.model_validate(REAL)

    def test_real_spec_wins_over_a_placeholder_copy(self) -> None:
        out = lift_agent_json(
            {"metadata": {"agent-json": REAL, "agent_json": json.dumps(REAL)}}
        )
        assert out["agent_json"].agent_name == "acme-agent"

    def test_falls_through_a_fresher_placeholder_to_a_real_copy(self) -> None:
        """A placeholder object next to a real serialized string is an agent
        run, not a direct one: freshness ranks the copies, validity picks the
        winner among them."""
        out = lift_agent_json(
            {
                "metadata": {
                    "agent-json": PLACEHOLDER,
                    "agent_json": json.dumps(REAL),
                }
            }
        )
        assert out["agent_json"].agent_name == "acme-agent"

    def test_valid_but_unpopulated_spec_is_still_promoted(self) -> None:
        """A name-only spec parses, so it is promoted and left for the SDR
        resolver's ``is_populated()`` check to reject."""
        out = lift_agent_json({"metadata": {"agent-json": {"agent-name": "acme"}}})
        assert out["agent_json"] == AgentCredentialSpec.model_validate(
            {"agent-name": "acme"}
        )

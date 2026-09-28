"""Unit tests for credential input routing (FND-2949)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError

from application_sdk.app.context import AppContext
from application_sdk.contracts.base import Input
from application_sdk.credentials import (
    CredentialMap,
    CredentialRef,
    InlineCredentials,
    ResolvedCredentials,
    expand_dotted_keys,
    find_prebuilt_credential_ref,
    flatten_dotted_keys,
    normalize_inline_credentials,
    route_credentials,
)
from application_sdk.credentials.errors import (
    CredentialParseError,
    CredentialResolvableTypeError,
    CredentialRoutingError,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.execution._temporal.preflight_gate import PreflightGateInput
from application_sdk.templates.contracts.sql_metadata import ExtractionInput
from application_sdk.testing import MockCredentialStore

_AGENT_SPEC = AgentCredentialSpec.model_validate(
    {
        "agent-name": "some-agent",
        "secret-manager": "awssecretmanager",
        "secret-path": "atlan/dev/test",
        "auth-type": "basic",
    }
)


class _AppInput(Input):
    """Shape of a toolkit-generated connector input."""

    credential_guid: str = ""
    extraction_method: str = "direct"
    agent_json: AgentCredentialSpec | None = None
    demo_credential: CredentialRef | None = None
    credentials: InlineCredentials = Field(default_factory=list)


class _TaskInput(Input):
    credential_ref: CredentialRef | None = None
    inline_credentials: CredentialMap = Field(default_factory=dict)


class _SourceLessInput(BaseModel):
    """No ``agent_json`` field, so not ``CredentialResolvable``."""

    credential_guid: str = ""
    credentials: InlineCredentials = Field(default_factory=list)


def _ref(name: str) -> CredentialRef:
    return CredentialRef(name=name, credential_type="basic")


# ---------------------------------------------------------------------------
# flatten_dotted_keys
# ---------------------------------------------------------------------------


class TestFlattenDottedKeys:
    def test_nested_sections_become_dotted_keys(self) -> None:
        nested = {"host": "h", "extra": {"client_id": "c", "auth": {"mode": "m"}}}
        assert flatten_dotted_keys(nested) == {
            "host": "h",
            "extra.client_id": "c",
            "extra.auth.mode": "m",
        }

    def test_flat_input_is_unchanged(self) -> None:
        flat = {"host": "h", "extra.client_id": "c"}
        assert flatten_dotted_keys(flat) == flat

    def test_round_trips_with_expand_dotted_keys(self) -> None:
        nested = {"host": "h", "port": 443, "extra": {"a": "1", "b": {"c": True}}}
        assert expand_dotted_keys(flatten_dotted_keys(nested)) == nested

    def test_two_paths_to_one_key_raise(self) -> None:
        with pytest.raises(ValueError, match="'extra.a' is given twice"):
            flatten_dotted_keys({"extra.a": 1, "extra": {"a": 2}})

    def test_non_string_key_raises(self) -> None:
        with pytest.raises(ValueError, match="must be strings"):
            flatten_dotted_keys({"extra": {1: "x"}})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


class TestCredentialContractTypes:
    def test_types_pass_payload_safety(self) -> None:
        # Defining an Input subclass runs the payload-safety validator; the
        # module-level _AppInput/_TaskInput above would already have raised.
        assert "credentials" in _AppInput.model_fields
        assert "inline_credentials" in _TaskInput.model_fields

    def test_credential_map_flattens_a_nested_dict(self) -> None:
        task = _TaskInput(inline_credentials={"extra": {"client_id": "c"}})  # type: ignore[arg-type]
        assert task.inline_credentials == {"extra.client_id": "c"}

    def test_credential_map_keeps_float(self) -> None:
        assert _TaskInput(inline_credentials={"timeout": 1.5}).inline_credentials == {
            "timeout": 1.5
        }

    def test_credential_map_refuses_a_structured_value(self) -> None:
        with pytest.raises(ValidationError):
            _TaskInput(inline_credentials={"scopes": ["a", "b"]})  # type: ignore[dict-item]

    def test_inline_credentials_accepts_pairs_and_nested_dict(self) -> None:
        pairs = _AppInput(credentials=[{"key": "host", "value": "h"}])
        assert pairs.credentials == [{"key": "host", "value": "h"}]
        nested = _AppInput(credentials={"extra": {"a": "1"}})  # type: ignore[arg-type]
        assert nested.credentials == {"extra.a": "1"}


# ---------------------------------------------------------------------------
# normalize_inline_credentials
# ---------------------------------------------------------------------------


class TestNormalizeInlineCredentials:
    def test_pairs_keep_dotted_keys(self) -> None:
        pairs = [
            {"key": "host", "value": "h"},
            {"key": "extra.client_id", "value": "c"},
        ]
        assert normalize_inline_credentials(pairs) == {
            "host": "h",
            "extra.client_id": "c",
        }

    def test_nested_dict_pairs_and_json_extra_land_on_the_same_keys(self) -> None:
        expected = {"host": "h", "extra.client_id": "c"}
        assert (
            normalize_inline_credentials(
                [
                    {"key": "host", "value": "h"},
                    {"key": "extra.client_id", "value": "c"},
                ]
            )
            == expected
        )
        assert (
            normalize_inline_credentials({"host": "h", "extra": {"client_id": "c"}})
            == expected
        )
        assert (
            normalize_inline_credentials({"host": "h", "extra": '{"client_id": "c"}'})
            == expected
        )

    def test_pairs_without_a_string_key_are_skipped(self) -> None:
        pairs: list[dict[str, object]] = [
            {"nokey": "x"},
            {"key": 7, "value": "x"},
            {"key": "host"},
        ]
        assert normalize_inline_credentials(pairs) == {"host": ""}

    def test_empty_inputs_normalize_to_empty(self) -> None:
        assert normalize_inline_credentials(None) == {}
        assert normalize_inline_credentials([]) == {}
        assert normalize_inline_credentials({}) == {}

    def test_malformed_json_extra_raises(self) -> None:
        with pytest.raises(CredentialParseError):
            normalize_inline_credentials({"extra": "{not json"})

    def test_structured_value_raises_naming_the_key(self) -> None:
        with pytest.raises(CredentialParseError) as exc_info:
            normalize_inline_credentials({"scopes": ["a"]})
        assert exc_info.value.credential_name == "scopes"

    def test_conflicting_keys_raise(self) -> None:
        with pytest.raises(CredentialParseError, match="given twice"):
            normalize_inline_credentials({"extra.a": "1", "extra": {"a": "2"}})


# ---------------------------------------------------------------------------
# find_prebuilt_credential_ref
# ---------------------------------------------------------------------------


class TestFindPrebuiltCredentialRef:
    def test_app_credential_field_is_found(self) -> None:
        ref = _ref("app")
        assert find_prebuilt_credential_ref(_AppInput(demo_credential=ref)) == ref

    def test_generic_credential_ref_wins(self) -> None:
        class _Both(_AppInput):
            credential_ref: CredentialRef | None = None

        generic = _ref("generic")
        found = find_prebuilt_credential_ref(
            _Both(credential_ref=generic, demo_credential=_ref("app"))
        )
        assert found == generic

    def test_several_app_refs_are_ambiguous(self) -> None:
        class _Two(_AppInput):
            other_credential: CredentialRef | None = None

        two = _Two(demo_credential=_ref("a"), other_credential=_ref("b"))
        with pytest.raises(CredentialRoutingError, match="several credential refs"):
            find_prebuilt_credential_ref(two)
        assert find_prebuilt_credential_ref(two, ref_field="other_credential") == _ref(
            "b"
        )

    def test_nothing_set_returns_none(self) -> None:
        assert find_prebuilt_credential_ref(_AppInput()) is None


# ---------------------------------------------------------------------------
# route_credentials
# ---------------------------------------------------------------------------


class TestRouteCredentials:
    def test_prebuilt_ref_wins_over_guid(self) -> None:
        ref = _ref("app")
        routed = route_credentials(_AppInput(demo_credential=ref, credential_guid="g"))
        assert routed == ResolvedCredentials(ref, {})

    def test_guid_routes_direct(self) -> None:
        ref, inline = route_credentials(_AppInput(credential_guid="g"))
        assert ref is not None and ref.credential_guid == "g"
        assert inline == {}

    def test_populated_agent_spec_routes_agent(self) -> None:
        ref, _ = route_credentials(
            _AppInput(extraction_method="agent", agent_json=_AGENT_SPEC)
        )
        assert ref is not None and ref.agent_spec == _AGENT_SPEC

    def test_miner_extraction_method_routes_by_guid(self) -> None:
        ref, _ = route_credentials(
            _AppInput(extraction_method="query_history", credential_guid="g")
        )
        assert ref is not None and ref.credential_guid == "g"

    def test_misrouted_agent_run_raises_instead_of_falling_back(self) -> None:
        # extraction_method=agent with an empty spec: the GUID must not be used,
        # and neither may the inline credentials.
        misrouted = _AppInput(
            extraction_method="agent",
            credential_guid="g",
            credentials=[{"key": "host", "value": "h"}],
        )
        with pytest.raises(CredentialRoutingError):
            route_credentials(misrouted)

    def test_inline_used_only_without_routing_fields(self) -> None:
        routed = route_credentials(
            _AppInput(credentials=[{"key": "extra.client_id", "value": "c"}])
        )
        assert routed == ResolvedCredentials(None, {"extra.client_id": "c"})

    def test_non_resolvable_input_with_guid_gets_a_guid_ref(self) -> None:
        ref, _ = route_credentials(_SourceLessInput(credential_guid="g"))
        assert ref is not None and ref.credential_guid == "g"

    def test_non_resolvable_input_falls_back_to_inline(self) -> None:
        routed = route_credentials(
            _SourceLessInput(credentials=[{"key": "host", "value": "h"}])
        )
        assert routed == ResolvedCredentials(None, {"host": "h"})

    def test_non_resolvable_input_with_only_an_agent_spec_raises(self) -> None:
        class _AgentOnly(BaseModel):
            agent_json: AgentCredentialSpec | None = None

        with pytest.raises(CredentialResolvableTypeError):
            route_credentials(_AgentOnly(agent_json=_AGENT_SPEC))

    def test_no_credential_at_all(self) -> None:
        assert route_credentials(_AppInput()) == ResolvedCredentials(None, {})

    def test_sdk_extraction_input_routes(self) -> None:
        ref, _ = route_credentials(ExtractionInput(credential_guid="g"))
        assert ref is not None and ref.credential_guid == "g"

    def test_custom_inline_field(self) -> None:
        class _Custom(BaseModel):
            creds: InlineCredentials = Field(default_factory=list)

        routed = route_credentials(
            _Custom(creds=[{"key": "host", "value": "h"}]), inline_field="creds"
        )
        assert routed.inline == {"host": "h"}

    def test_inline_output_fits_the_task_contract(self) -> None:
        _, inline = route_credentials(
            _AppInput(credentials={"host": "h", "extra": '{"client_id": "c"}'})  # type: ignore[arg-type]
        )
        assert _TaskInput(inline_credentials=inline).inline_credentials == inline


# ---------------------------------------------------------------------------
# AppContext.resolve_credential_raw_or_inline
# ---------------------------------------------------------------------------


class TestResolveCredentialRawOrInline:
    @pytest.mark.asyncio
    async def test_ref_path_resolves_through_the_store(self) -> None:
        store = MockCredentialStore()
        ref = store.add_basic("svc", username="u", password="p")
        ctx = AppContext(
            app_name="a", app_version="1", _secret_store=store.secret_store
        )
        raw = await ctx.resolve_credential_raw_or_inline(ref, {"host": "ignored"})
        assert raw.get("username") == "u"

    @pytest.mark.asyncio
    async def test_inline_path_returns_the_nested_shape(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        raw = await ctx.resolve_credential_raw_or_inline(
            None, {"host": "h", "extra.client_id": "c"}
        )
        assert raw == {"host": "h", "extra": {"client_id": "c"}}

    @pytest.mark.asyncio
    async def test_neither_raises(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(CredentialRoutingError, match="neither"):
            await ctx.resolve_credential_raw_or_inline(None, {})


# ---------------------------------------------------------------------------
# Preflight gate agrees with the tasks on the pre-built ref
# ---------------------------------------------------------------------------


class TestGatePrebuiltRef:
    def test_gate_carries_the_app_credential_field(self) -> None:
        ref = _ref("app")
        gate = PreflightGateInput.from_extraction_input(
            _AppInput(demo_credential=ref), "extract"
        )
        assert gate.credential_ref == ref

    def test_ambiguous_refs_fall_back_to_credential_ref_only(self) -> None:
        class _Two(_AppInput):
            other_credential: CredentialRef | None = None

        gate = PreflightGateInput.from_extraction_input(
            _Two(demo_credential=_ref("a"), other_credential=_ref("b")), "extract"
        )
        assert gate.credential_ref is None

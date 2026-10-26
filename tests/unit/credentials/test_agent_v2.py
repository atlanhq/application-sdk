"""Resolution of ``format: 2`` agent_json payloads (per-field value sources)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from application_sdk.credentials.agent import (
    check_secret_store_access,
    resolve_agent_credential,
)
from application_sdk.credentials.errors import (
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.ingress import normalize_agent_json
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors import Audience, FailureCategory
from application_sdk.errors.leaves import (
    ColdStartRaceError,
    DaprSidecarUnreachableError,
)
from application_sdk.execution._temporal.converter import create_data_converter
from application_sdk.infrastructure.secrets import (
    AgentSecretReadError,
    SecretStoreError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)
from application_sdk.templates.contracts.sql_metadata import ExtractionInput
from application_sdk.testing.mocks import MockSecretStore

_SECRET_NAME = "connections/example-db"
_SECRET_VALUE = "example-password-value"


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": 2,
        "agent-name": "example-agent",
        "secret-manager": "awssecretmanager",
        "auth-type": "basic",
        "host": {"value": "db.example.com"},
        "port": {"value": 5432},
        "basic.username": {"value": "example_user"},
        "basic.password": {
            "valueFrom": {"secretKeyRef": {"name": _SECRET_NAME, "key": "db_password"}}
        },
        "extra.ca_cert": {"valueFrom": {"fileRef": {"path": "objectstore://c/ca.pem"}}},
    }
    base.update(overrides)
    return base


def _store() -> MockSecretStore:
    return MockSecretStore({_SECRET_NAME: json.dumps({"db_password": _SECRET_VALUE})})


def _spec(payload: dict[str, Any]) -> AgentCredentialSpec:
    spec = normalize_agent_json(payload)
    assert spec is not None
    return spec


class _FailingStore(MockSecretStore):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def get(self, name: str) -> str:
        raise self._exc


class TestResolve:
    async def test_resolves_literals_secret_and_file_ref(self) -> None:
        out = await resolve_agent_credential(_spec(_payload()), _store())
        assert out["host"] == "db.example.com"
        assert out["port"] == 5432  # int preserved, not stringified
        assert out["username"] == "example_user"
        assert out["password"] == _SECRET_VALUE
        assert out["extra"] == {"ca_cert": "objectstore://c/ca.pem"}
        assert out["authType"] == "basic"
        assert "format" not in out

    async def test_envelope_name_is_kept_as_a_plain_value(self) -> None:
        payload = _payload(name="atlan-mysql-synthetic-id-abc123")
        out = await resolve_agent_credential(_spec(payload), _store())
        assert out["name"] == "atlan-mysql-synthetic-id-abc123"

    async def test_one_secret_is_fetched_once_for_many_keys(self) -> None:
        store = MockSecretStore(
            {_SECRET_NAME: json.dumps({"db_password": "p", "token": "t"})}
        )
        payload = _payload(
            token={
                "valueFrom": {"secretKeyRef": {"name": _SECRET_NAME, "key": "token"}}
            }
        )
        out = await resolve_agent_credential(_spec(payload), store)
        assert (out["password"], out["token"]) == ("p", "t")
        assert store._get_calls == [_SECRET_NAME]

    async def test_unset_key_type_default_does_not_conflict_with_v2(self) -> None:
        # The spec's defaults (key-type "", secret-path "") must not be read as
        # v1 fetch keys: only keys the payload set are parsed.
        spec = _spec(_payload())
        assert spec.to_raw_dict()["key-type"] == ""
        await resolve_agent_credential(spec, _store())

    async def test_explicit_v1_fetch_key_is_rejected(self) -> None:
        with pytest.raises(CredentialParseError, match="key-type"):
            await resolve_agent_credential(
                _spec(_payload(**{"key-type": "single-key"})), _store()
            )

    async def test_invalid_source_names_the_field_only(self) -> None:
        payload = _payload(**{"basic.username": "example_user"})
        with pytest.raises(CredentialParseError) as info:
            await resolve_agent_credential(_spec(payload), _store())
        assert "basic.username" in str(info.value)
        assert "example_user" not in str(info.value)


class TestAbsence:
    async def test_missing_secret_is_credential_not_found_without_cause(self) -> None:
        with pytest.raises(CredentialNotFoundError) as info:
            await resolve_agent_credential(_spec(_payload()), MockSecretStore())
        # The preflight gate reads "no cause" as a definitive absence.
        assert info.value.__cause__ is None
        assert _SECRET_NAME not in str(info.value)
        assert "basic.password" in str(info.value)

    async def test_missing_key_is_credential_not_found(self) -> None:
        store = MockSecretStore({_SECRET_NAME: json.dumps({"other": "x"})})
        with pytest.raises(CredentialNotFoundError) as info:
            await resolve_agent_credential(_spec(_payload()), store)
        assert "db_password" not in str(info.value)
        assert _SECRET_NAME not in str(info.value)

    @pytest.mark.parametrize("body", ["not json", "[1, 2]"])
    async def test_non_map_secret_with_key_not_name_is_not_found(
        self, body: str
    ) -> None:
        store = MockSecretStore({_SECRET_NAME: body})
        with pytest.raises(CredentialNotFoundError) as info:
            await resolve_agent_credential(_spec(_payload()), store)
        assert _SECRET_NAME not in str(info.value)
        assert info.value.__cause__ is None


def _single_value_payload(name: str, key: str) -> dict[str, Any]:
    return {
        "format": 2,
        "agent-name": "example-agent",
        "token": {"valueFrom": {"secretKeyRef": {"name": name, "key": key}}},
    }


class TestSingleValueSecret:
    async def test_plain_string_with_key_equal_name_returns_the_string(self) -> None:
        store = MockSecretStore({"example-pw": "plain-secret"})
        out = await resolve_agent_credential(
            _spec(_single_value_payload("example-pw", "example-pw")), store
        )
        assert out["token"] == "plain-secret"

    async def test_numeric_string_stays_a_string(self) -> None:
        store = MockSecretStore({"example-pw": "12345"})
        out = await resolve_agent_credential(
            _spec(_single_value_payload("example-pw", "example-pw")), store
        )
        assert out["token"] == "12345"
        assert isinstance(out["token"], str)

    async def test_plain_string_with_key_not_name_is_not_found(self) -> None:
        store = MockSecretStore({"example-pw": "plain-secret"})
        with pytest.raises(CredentialNotFoundError) as info:
            await resolve_agent_credential(
                _spec(_single_value_payload("example-pw", "other")), store
            )
        assert "plain-secret" not in str(info.value)
        assert info.value.__cause__ is None

    async def test_json_map_key_hit_returns_the_value(self) -> None:
        store = MockSecretStore({"example-pw": json.dumps({"k": "v"})})
        out = await resolve_agent_credential(
            _spec(_single_value_payload("example-pw", "k")), store
        )
        assert out["token"] == "v"

    async def test_json_map_key_miss_with_key_not_name_is_not_found(self) -> None:
        store = MockSecretStore({"example-pw": json.dumps({"k": "v"})})
        with pytest.raises(CredentialNotFoundError):
            await resolve_agent_credential(
                _spec(_single_value_payload("example-pw", "other")), store
            )

    async def test_two_fields_on_one_name_fetch_once(self) -> None:
        store = MockSecretStore({"example-pw": json.dumps({"a": "1", "b": "2"})})
        payload = _single_value_payload("example-pw", "a")
        payload["token2"] = {
            "valueFrom": {"secretKeyRef": {"name": "example-pw", "key": "b"}}
        }
        out = await resolve_agent_credential(_spec(payload), store)
        assert (out["token"], out["token2"]) == ("1", "2")
        assert store._get_calls == ["example-pw"]


class TestStoreFailure:
    async def test_generic_failure_is_agent_secret_read_error(self) -> None:
        source = SecretStoreError("denied for " + _SECRET_NAME, retryable=True)
        with pytest.raises(AgentSecretReadError) as info:
            await resolve_agent_credential(_spec(_payload()), _FailingStore(source))
        err = info.value
        assert isinstance(err, SecretStoreError)
        assert err.category is FailureCategory.PRECONDITION
        assert err.audience is Audience.USER
        assert err.code.startswith("PRECONDITION_")
        assert err.effective_retryable is True  # carried from the source
        assert err.__cause__ is None
        assert _SECRET_NAME not in str(err)
        assert "basic.password" in str(err)

    async def test_non_retryable_source_stays_non_retryable(self) -> None:
        source = SecretStoreError("denied", retryable=False)
        with pytest.raises(AgentSecretReadError) as info:
            await resolve_agent_credential(_spec(_payload()), _FailingStore(source))
        assert info.value.effective_retryable is False

    async def test_cold_start_outage_keeps_its_subtype_keyed_by_field(self) -> None:
        source = SecretStoreUnavailableError(_SECRET_NAME)
        assert isinstance(source, ColdStartRaceError)
        with pytest.raises(SecretStoreUnavailableError) as info:
            await resolve_agent_credential(_spec(_payload()), _FailingStore(source))
        assert _SECRET_NAME not in str(info.value)
        assert info.value.__cause__ is None

    async def test_terminal_outage_keeps_its_subtype_keyed_by_field(self) -> None:
        source = SecretStoreUnreachableError(_SECRET_NAME, component="c", attempts=3)
        assert isinstance(source, DaprSidecarUnreachableError)
        with pytest.raises(SecretStoreUnreachableError) as info:
            await resolve_agent_credential(_spec(_payload()), _FailingStore(source))
        assert _SECRET_NAME not in str(info.value)
        assert info.value.attempts == 3


class TestSpecIngress:
    def test_v2_dict_host_and_port_are_accepted_and_populated(self) -> None:
        spec = _spec(_payload())
        assert spec.is_v2
        assert spec.is_populated()
        assert spec.host == {"value": "db.example.com"}

    def test_dict_host_is_rejected_when_not_v2(self) -> None:
        legacy = {"agent-name": "a", "secret-path": "p", "host": {"value": "h"}}
        assert normalize_agent_json(legacy) is None

    def test_dict_port_is_rejected_when_format_is_not_exactly_int_2(self) -> None:
        for fmt in ("2", 2.0, True, 1):
            assert normalize_agent_json(_payload(format=fmt)) is None

    def test_legacy_placeholder_is_still_absent(self) -> None:
        assert (
            normalize_agent_json({"agent-name": "agent-name", "port": "port"}) is None
        )

    def test_v2_without_agent_name_is_not_populated(self) -> None:
        assert not _spec(_payload(**{"agent-name": ""})).is_populated()


class TestPreflightProbe:
    async def test_v2_passes_and_returns_resolved(self) -> None:
        result = await check_secret_store_access(_spec(_payload()), _store())
        assert result.passed and not result.fatal
        assert result.substituted == 1
        assert result.resolved is not None
        assert result.resolved["password"] == _SECRET_VALUE

    async def test_v2_missing_secret_is_fatal_not_store_down(self) -> None:
        result = await check_secret_store_access(_spec(_payload()), MockSecretStore())
        assert result.fatal and not result.store_down
        assert _SECRET_NAME not in result.message

    async def test_v2_outage_is_store_down(self) -> None:
        store = _FailingStore(SecretStoreUnavailableError("x"))
        result = await check_secret_store_access(_spec(_payload()), store)
        assert result.fatal and result.store_down


class TestBoundary:
    """raw dict -> ExtractionInput -> Temporal converter -> ref -> resolver."""

    async def _through_the_wire(self, payload: dict[str, Any]) -> ExtractionInput:
        original = ExtractionInput.model_validate(
            {"extraction_method": "agent", "agent_json": payload}
        )
        converter = create_data_converter()
        encoded = await converter.encode([original])
        (decoded,) = await converter.decode(encoded, [ExtractionInput])
        return decoded

    async def test_v2_payload_survives_the_wire_and_resolves(self) -> None:
        decoded = await self._through_the_wire(_payload())
        assert decoded.agent_json is not None and decoded.agent_json.is_v2
        ref = CredentialRef.resolve(decoded)
        assert ref.agent_spec is not None
        out = await CredentialResolver(_store()).resolve_raw(ref)
        assert out["password"] == _SECRET_VALUE
        assert out["port"] == 5432

    async def test_payload_without_host_or_port_survives_the_wire(self) -> None:
        # A REST-style app: envelope keys plus one secret, no host/port.
        store = MockSecretStore({"example-api": json.dumps({"token": "t0k"})})
        payload = {
            "format": 2,
            "agent-name": "example-agent",
            "api.token": {
                "valueFrom": {"secretKeyRef": {"name": "example-api", "key": "token"}}
            },
        }
        decoded = await self._through_the_wire(payload)
        ref = CredentialRef.resolve(decoded)
        out = await CredentialResolver(store).resolve_raw(ref)
        assert out["api.token"] == "t0k"

    async def test_missing_secret_surfaces_as_not_found(self) -> None:
        decoded = await self._through_the_wire(_payload())
        ref = CredentialRef.resolve(decoded)
        with pytest.raises(CredentialNotFoundError):
            await CredentialResolver(MockSecretStore()).resolve_raw(ref)

    async def test_resolver_does_not_fold_the_read_error_into_not_found(self) -> None:
        decoded = await self._through_the_wire(_payload())
        ref = CredentialRef.resolve(decoded)
        store = _FailingStore(SecretStoreError("denied", retryable=False))
        with pytest.raises(AgentSecretReadError) as info:
            await CredentialResolver(store).resolve_raw(ref)
        assert not isinstance(info.value, CredentialNotFoundError)

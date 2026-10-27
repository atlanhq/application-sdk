"""Declared-source ``agent_json`` (``"format": 2``) — FND-4361 / FND-4407.

Every field states where its value comes from, so resolution never guesses:

* ``{"value": X}`` — a literal, never sent to the secret store;
* ``{"valueFrom": {"secretKeyRef": {"name", "key"}}}`` — looked up strictly;
* ``{"valueFrom": {"fileRef": {"path"}}}`` — a file reference.

The spec normalizes the payload at ingress into today's flat shape plus a
``value-sources`` map, so apps see exactly the credentials a legacy payload
gives them. These tests pin that equivalence, the round trips apps rely on,
and the census failure classes: a literal the store rejects as a name can no
longer fail a declared payload, because literals never reach the store.
"""

from __future__ import annotations

import json
from typing import Any

import orjson
import pytest
from pydantic import ConfigDict, Field

from application_sdk.credentials.agent import (
    check_secret_store_access,
    resolve_agent_credential,
)
from application_sdk.credentials.errors import (
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.handler.contracts import HandlerCredential
from application_sdk.infrastructure.secrets import (
    AgentSecretReadError,
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)


@pytest.fixture(autouse=True)
def _no_cold_start_wait(monkeypatch):
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        0.0,
    )


class RecordingStore:
    """A store that holds *secrets* and rejects every other name the way a
    strict backend does (Azure Key Vault ``BadParameter``, a Kubernetes
    invalid name, a scoped identity's access denied): a store *answer* that
    is not "not found". Records every name it is asked for."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}
        self.calls: list[str] = []

    async def get(self, name: str) -> str:
        self.calls.append(name)
        if name in self.secrets:
            return self.secrets[name]
        raise SecretStoreError(
            "Secret store failed to return the secret: AccessDeniedException",
            secret_name=name,
            retryable=True,
        )

    async def get_optional(self, name: str) -> str | None:
        return await self.get(name)


def _v2(**fields: Any) -> dict[str, Any]:
    return {
        "format": 2,
        "agent-name": "sdr-agent",
        "secret-manager": "awssecretmanager",
        "aws-region": "us-east-1",
        "auth-type": "basic",
        **fields,
    }


def _ref(name: str, key: str | None = None) -> dict[str, Any]:
    return {"valueFrom": {"secretKeyRef": {"name": name, "key": key or name}}}


# ---------------------------------------------------------------------------
# Parsing — normalized to the flat shape apps already read
# ---------------------------------------------------------------------------


class TestParse:
    def test_values_flatten_and_keep_their_type(self) -> None:
        spec = AgentCredentialSpec.model_validate(
            _v2(
                host={"value": "db.example.com"},
                port={"value": 3306},
                **{"basic.username": {"value": "svc_user"}},
            )
        )
        assert spec.host == "db.example.com"
        assert spec.port == 3306
        raw = spec.to_raw_dict()
        assert raw["basic.username"] == "svc_user"
        assert raw["agent-name"] == "sdr-agent"
        assert "format" not in raw
        # Nothing to look up: the map is present but empty.
        assert spec.value_sources == {}
        assert raw["value-sources"] == {}

    def test_secret_ref_records_the_source_and_flattens_to_its_name(self) -> None:
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql", "password")})
        )
        assert spec.value_sources == {
            "basic.password": {"name": "prod/mysql", "key": "password"}
        }
        assert spec.to_raw_dict()["basic.password"] == "prod/mysql"

    def test_file_ref_flattens_to_its_path(self) -> None:
        spec = AgentCredentialSpec.model_validate(
            _v2(
                **{
                    "mtls.extra.cert": {
                        "valueFrom": {"fileRef": {"path": "objectstore://certs/a.pem"}}
                    }
                }
            )
        )
        assert spec.to_raw_dict()["mtls.extra.cert"] == "objectstore://certs/a.pem"
        assert spec.value_sources == {}

    def test_json_string_payload(self) -> None:
        spec = AgentCredentialSpec.model_validate(
            json.dumps(_v2(**{"basic.username": {"value": "u"}}))
        )
        assert spec.to_raw_dict()["basic.username"] == "u"

    def test_is_populated_without_key_type_or_secret_path(self) -> None:
        """Apps gate SDR routing on ``is_populated()``; a declared payload has
        neither ``key-type`` nor ``secret-path`` and must still route."""
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.username": {"value": "u"}})
        )
        assert spec.is_populated()

    @pytest.mark.parametrize(
        "field",
        [
            {"value": "a", "valueFrom": {"fileRef": {"path": "p"}}},
            {},
            {"valueFrom": {}},
            {
                "valueFrom": {
                    "fileRef": {"path": "p"},
                    "secretKeyRef": {"name": "n", "key": "k"},
                }
            },
            {"valueFrom": {"secretKeyRef": {"name": "n"}}},
            {"valueFrom": {"secretKeyRef": {"name": "", "key": "k"}}},
            {"valueFrom": {"configMapRef": {"name": "n"}}},
            {"valueFrom": {"fileRef": {}}},
            "plain-string",
            {"value": "a", "extra": 1},
        ],
    )
    def test_field_must_declare_exactly_one_source(self, field: Any) -> None:
        with pytest.raises(CredentialParseError, match="basic.username"):
            AgentCredentialSpec.model_validate(_v2(**{"basic.username": field}))

    @pytest.mark.parametrize("key", ["key-type", "secret-path"])
    def test_legacy_anchors_are_rejected(self, key: str) -> None:
        with pytest.raises(CredentialParseError, match=key):
            AgentCredentialSpec.model_validate(_v2(**{key: "single-key"}))

    def test_unknown_format_is_rejected(self) -> None:
        with pytest.raises(CredentialParseError, match="format"):
            AgentCredentialSpec.model_validate({"format": 3, "agent-name": "a"})

    def test_parse_errors_never_echo_values(self) -> None:
        with pytest.raises(CredentialParseError) as exc_info:
            AgentCredentialSpec.model_validate(
                _v2(**{"basic.password": "hunter2-literal"})
            )
        assert "hunter2-literal" not in str(exc_info.value)


#: A format-2 payload shaped like the real v2 form's output: besides the
#: envelope keys, the form and the vendored agent configmap send ``agent-type``,
#: ``sdk-version``, ``aws-assume-role-arn``, ``azure-vault-name`` and
#: ``agent-custom-config`` as plain values.
FORM_PAYLOAD: dict[str, Any] = {
    "format": 2,
    "agent-name": "sdr-agent",
    "agent-type": "new-app-framework",
    "sdk-version": "3.53.0",
    "secret-manager": "awssecretmanager",
    "aws-region": "us-east-1",
    "aws-auth-method": "iam-assume-role",
    "aws-assume-role-arn": "arn:aws:iam::123456789012:role/sdr",
    "azure-vault-name": "",
    "agent-custom-config": {"images": {"app": "example"}},
    "auth-type": "basic",
    "connectBy": "host",
    "host": {"value": "db.example.com"},
    "port": {"value": 3306},
    "basic.username": {"value": "svc_user"},
    "basic.password": _ref("prod/mysql", "password"),
    "extra.ca_cert": {"valueFrom": {"fileRef": {"path": "objectstore://ca.pem"}}},
}


class TestFormPayload:
    def test_the_forms_extra_envelope_keys_pass_through(self) -> None:
        spec = AgentCredentialSpec.model_validate(FORM_PAYLOAD)
        raw = spec.to_raw_dict()
        assert raw["agent-type"] == "new-app-framework"
        assert raw["sdk-version"] == "3.53.0"
        assert raw["aws-assume-role-arn"] == "arn:aws:iam::123456789012:role/sdr"
        assert raw["agent-custom-config"] == {"images": {"app": "example"}}
        assert spec.value_sources == {
            "basic.password": {"name": "prod/mysql", "key": "password"}
        }


class TestPublishedSchema:
    """Heracles validates workflow inputs against the app's published input
    contract (the SDK's JSON schema) on create and update. A format-2
    ``agent_json`` must pass that validation, or no form can save one."""

    @staticmethod
    def _validate(schema: dict[str, Any], instance: Any) -> None:
        import jsonschema

        jsonschema.validate(instance, schema)

    def test_spec_schema_accepts_format_2(self) -> None:
        self._validate(AgentCredentialSpec.model_json_schema(), FORM_PAYLOAD)

    def test_spec_schema_still_accepts_legacy(self) -> None:
        self._validate(
            AgentCredentialSpec.model_json_schema(),
            {
                "agent-name": "a",
                "key-type": "single-key",
                "host": "db",
                "port": 3306,
                "basic.username": "u",
            },
        )

    def test_extraction_input_contract_accepts_format_2(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        self._validate(
            ExtractionInput.model_json_schema(),
            {"extraction_method": "agent", "agent_json": FORM_PAYLOAD},
        )

    def test_typed_app_subclass_schema_accepts_format_2(self) -> None:
        self._validate(StrictAppSpec.model_json_schema(), FORM_PAYLOAD)

    @pytest.mark.parametrize(
        "bad_field",
        [
            {"bogus": 1},
            {"value": 1, "valueFrom": {"fileRef": {"path": "p"}}},
            {"valueFrom": {"secretKeyRef": {"name": "n"}}},
        ],
    )
    def test_schema_rejects_a_malformed_source(self, bad_field: Any) -> None:
        import jsonschema

        with pytest.raises(jsonschema.ValidationError):
            self._validate(
                AgentCredentialSpec.model_json_schema(),
                {**FORM_PAYLOAD, "basic.password": bad_field},
            )


class TestRoundTrip:
    """Apps re-serialize the spec (``to_raw_dict``, Temporal payloads) and
    re-parse it in later activities. The declared sources must survive, or the
    re-parsed payload would fall back to legacy probing."""

    def _spec(self) -> AgentCredentialSpec:
        return AgentCredentialSpec.model_validate(
            _v2(
                host={"value": "db.example.com"},
                **{
                    "basic.username": {"value": "svc_user"},
                    "basic.password": _ref("prod/mysql", "password"),
                },
            )
        )

    def test_to_raw_dict_round_trip(self) -> None:
        spec = self._spec()
        again = AgentCredentialSpec.model_validate(spec.to_raw_dict())
        assert again.value_sources == spec.value_sources
        assert again.to_raw_dict() == spec.to_raw_dict()

    def test_json_round_trip(self) -> None:
        spec = self._spec()
        again = AgentCredentialSpec.model_validate(
            orjson.dumps(spec.model_dump(mode="json", by_alias=True)).decode()
        )
        assert again.value_sources == spec.value_sources

    def test_model_dump_without_alias_round_trip(self) -> None:
        spec = self._spec()
        again = AgentCredentialSpec.model_validate(spec.model_dump(mode="json"))
        assert again.value_sources == spec.value_sources

    def test_legacy_payload_dump_is_unchanged(self) -> None:
        """A legacy payload grows no new key, so its serialized form — read by
        apps and by older SDKs in child workflows — stays byte-identical."""
        legacy = {
            "agent-name": "sdr-agent",
            "key-type": "single-key",
            "basic.username": "u",
        }
        spec = AgentCredentialSpec.model_validate(legacy)
        assert "value-sources" not in spec.to_raw_dict()
        assert "value_sources" not in spec.model_dump()
        assert "value-sources" not in spec.model_dump(by_alias=True)
        assert spec.value_sources is None

    def test_spec_stays_hashable(self) -> None:
        """The spec is frozen, and frozen models get hashed (as a field of a
        hashed ``CredentialRef``, or a cache key); a dict field must not break
        that."""
        from application_sdk.credentials.ref import CredentialRef

        spec = self._spec()
        assert hash(spec) == hash(
            AgentCredentialSpec.model_validate(spec.to_raw_dict())
        )
        hash(CredentialRef(agent_spec=spec))
        hash(StrictAppSpec.model_validate(spec.to_raw_dict()))

    def test_serialization_schema_keeps_the_fields(self) -> None:
        """The serializer that omits an absent ``value-sources`` must not blank
        the published schema of the spec, or of every input embedding it."""
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        props = AgentCredentialSpec.model_json_schema(mode="serialization")[
            "properties"
        ]
        assert {"agent-name", "host", "port", "value-sources"} <= set(props)
        embedded = ExtractionInput.model_json_schema(mode="serialization")["$defs"][
            "AgentCredentialSpec"
        ]
        assert "agent-name" in embedded["properties"]

    def test_malformed_value_sources_is_rejected(self) -> None:
        raw = self._spec().to_raw_dict()
        raw["value-sources"] = {"basic.password": {"name": "n"}}
        with pytest.raises(CredentialParseError, match="basic.password"):
            AgentCredentialSpec.model_validate(raw)

    def test_value_sources_naming_a_missing_field_is_rejected(self) -> None:
        raw = self._spec().to_raw_dict()
        raw["value-sources"]["basic.token"] = {"name": "n", "key": "k"}
        with pytest.raises(CredentialParseError, match="basic.token"):
            AgentCredentialSpec.model_validate(raw)


class TestIngress:
    """Through the real ingress, not just ``model_validate``: workflow inputs
    reach the spec via ``normalize_agent_json`` inside the input model."""

    def _payload(self) -> dict[str, Any]:
        return _v2(
            port={"value": 3306},
            **{
                "basic.username": {"value": "svc_user"},
                "basic.password": _ref("prod/mysql", "password"),
            },
        )

    def test_normalize_agent_json_keeps_the_sources(self) -> None:
        from application_sdk.credentials import normalize_agent_json

        spec = normalize_agent_json(json.dumps(self._payload()))
        assert spec is not None
        assert spec.is_populated()
        assert spec.value_sources == {
            "basic.password": {"name": "prod/mysql", "key": "password"}
        }

    def test_extraction_input_round_trip(self) -> None:
        """The workflow input is serialized between activities; the nested
        spec must come back still declaring its sources."""
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        first = ExtractionInput.model_validate(
            {"extraction_method": "agent", "agent_json": self._payload()}
        )
        assert first.agent_json is not None
        again = ExtractionInput.model_validate(first.model_dump(mode="json"))
        assert again.agent_json is not None
        assert again.agent_json.value_sources == first.agent_json.value_sources
        assert again.agent_json.port == 3306

    def test_malformed_declared_payload_is_loud_not_absent(self) -> None:
        """Ingress reads an invalid legacy blob as "no agent reference" (the
        marketplace placeholder). A payload that opted into declared sources
        is never a placeholder: reading it as absent would silently route the
        run to the GUID path."""
        from application_sdk.credentials import normalize_agent_json

        broken = _v2(**{"basic.password": "not-a-source-object"})
        with pytest.raises(CredentialParseError, match="basic.password"):
            normalize_agent_json(broken)
        with pytest.raises(CredentialParseError):
            normalize_agent_json(json.dumps(broken))

    def test_invalid_legacy_placeholder_is_still_absent(self) -> None:
        from application_sdk.credentials import normalize_agent_json

        assert (
            normalize_agent_json({"agent-name": "agent-name", "port": "port"}) is None
        )


class StrictAppSpec(AgentCredentialSpec):
    """An app-owned spec: declared, typed fields and ``extra="forbid"``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    username: str = Field(default="", alias="basic.username")
    password: str = Field(default="", alias="basic.password")


class TestAppSubclass:
    def test_typed_forbid_subclass_accepts_format_2(self) -> None:
        spec = StrictAppSpec.model_validate(
            _v2(
                port={"value": 1521},
                **{
                    "basic.username": {"value": "svc_user"},
                    "basic.password": _ref("prod/oracle"),
                },
            )
        )
        assert spec.username == "svc_user"
        assert spec.password == "prod/oracle"
        assert spec.port == 1521
        again = StrictAppSpec.model_validate(spec.to_raw_dict())
        assert again.value_sources == spec.value_sources


# ---------------------------------------------------------------------------
# Resolution — only declared refs reach the store
# ---------------------------------------------------------------------------


class TestResolve:
    async def test_literals_never_reach_the_store(self) -> None:
        """The census classes (a plain username, ``extra.compiled_url``, a role
        ARN, an ``objectstore://`` path) each failed because the store was
        asked about a literal. A declared payload never asks."""
        store = RecordingStore({"prod/mysql": "s3cret"})
        spec = AgentCredentialSpec.model_validate(
            _v2(
                host={"value": "db.example.com"},
                port={"value": 3306},
                **{
                    "basic.username": {"value": "svc_user"},
                    "basic.password": _ref("prod/mysql"),
                    "extra.compiled_url": {"value": "mysql://db.example.com/sales"},
                    "extra.database": {"value": "sales_db.prod"},
                    "role.extra.aws_role_arn": {
                        "value": "arn:aws:iam::123456789012:role/atlan"
                    },
                },
            )
        )
        resolved = await resolve_agent_credential(spec, store)  # type: ignore[arg-type]
        assert store.calls == ["prod/mysql"]
        assert resolved["password"] == "s3cret"
        assert resolved["username"] == "svc_user"
        assert resolved["port"] == 3306
        assert "value-sources" not in resolved

    async def test_all_literal_payload_makes_no_store_call(self) -> None:
        store = RecordingStore()
        spec = AgentCredentialSpec.model_validate(
            _v2(
                **{
                    "basic.username": {"value": "u"},
                    "basic.password": {"value": "p"},
                }
            )
        )
        resolved = await resolve_agent_credential(spec, store)  # type: ignore[arg-type]
        assert store.calls == []
        assert resolved["username"] == "u"
        assert resolved["password"] == "p"

    async def test_equivalent_to_the_legacy_payload(self) -> None:
        """Same credentials, either shape: the handler sees identical pairs."""
        legacy = AgentCredentialSpec.model_validate(
            {
                "agent-name": "sdr-agent",
                "secret-manager": "awssecretmanager",
                "aws-region": "us-east-1",
                "auth-type": "basic",
                "key-type": "single-key",
                "host": "db.example.com",
                "port": 3306,
                "basic.username": "prod/mysql-user",
                "basic.password": "prod/mysql-pass",
                "extra.database": "sales",
            }
        )

        class LegacyStore:
            async def get_optional(self, name: str) -> str | None:
                return {"prod/mysql-user": "u", "prod/mysql-pass": "p"}.get(name)

        declared = AgentCredentialSpec.model_validate(
            _v2(
                host={"value": "db.example.com"},
                port={"value": 3306},
                **{
                    "basic.username": _ref("prod/mysql-user"),
                    "basic.password": _ref("prod/mysql-pass"),
                    "extra.database": {"value": "sales"},
                },
            )
        )
        store = RecordingStore({"prod/mysql-user": "u", "prod/mysql-pass": "p"})

        legacy_out = await resolve_agent_credential(legacy, LegacyStore())  # type: ignore[arg-type]
        declared_out = await resolve_agent_credential(declared, store)  # type: ignore[arg-type]
        # Only the ``key-type`` envelope key differs ("single-key" vs the
        # model default ""); every credential field the handler reads is
        # identical.
        assert legacy_out.pop("key-type") == "single-key"
        assert declared_out.pop("key-type") == ""
        assert declared_out == legacy_out
        assert HandlerCredential.list_from_raw(
            declared_out
        ) == HandlerCredential.list_from_raw(legacy_out)

    async def test_one_lookup_per_secret_name_and_key_picks_the_field(self) -> None:
        store = RecordingStore(
            {"prod/mysql": json.dumps({"username": "u", "password": "p"})}
        )
        spec = AgentCredentialSpec.model_validate(
            _v2(
                **{
                    "basic.username": _ref("prod/mysql", "username"),
                    "basic.password": _ref("prod/mysql", "password"),
                }
            )
        )
        resolved = await resolve_agent_credential(spec, store)  # type: ignore[arg-type]
        assert store.calls == ["prod/mysql"]
        assert resolved["username"] == "u"
        assert resolved["password"] == "p"

    async def test_single_value_secret_key_equals_name(self) -> None:
        """Single-value stores (Azure Key Vault, GCP, a plain AWS string):
        ``key`` is the name and the whole value is the secret."""
        store = RecordingStore({"db-password": "p"})
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("db-password")})
        )
        resolved = await resolve_agent_credential(spec, store)  # type: ignore[arg-type]
        assert resolved["password"] == "p"

    async def test_missing_key_in_a_bundle_secret_is_not_found(self) -> None:
        store = RecordingStore({"prod/mysql": json.dumps({"username": "u"})})
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql", "password")})
        )
        with pytest.raises(CredentialNotFoundError) as exc_info:
            await resolve_agent_credential(spec, store)  # type: ignore[arg-type]
        assert "basic.password" in str(exc_info.value)
        assert "prod/mysql" not in str(exc_info.value)

    async def test_declared_ref_not_found_is_a_user_error(self) -> None:
        class EmptyStore:
            async def get(self, name: str) -> str:
                raise SecretNotFoundError(name)

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/missing")})
        )
        with pytest.raises(CredentialNotFoundError) as exc_info:
            await resolve_agent_credential(spec, EmptyStore())  # type: ignore[arg-type]
        assert "basic.password" in str(exc_info.value)
        assert "prod/missing" not in str(exc_info.value)

    async def test_declared_ref_store_failure_fails_the_run(self) -> None:
        """No fallback for a declared ref: the field is known to be a secret,
        so sending its name as the value would be wrong (FND-4077)."""
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/denied")})
        )
        with pytest.raises(AgentSecretReadError) as exc_info:
            await resolve_agent_credential(spec, RecordingStore())  # type: ignore[arg-type]
        err = exc_info.value
        # The customer's store refused a field known to be a ref: theirs to fix.
        assert err.category is FailureCategory.PRECONDITION
        assert err.audience is Audience.USER
        assert err.suggested_action
        assert "basic.password" in str(err)
        assert "prod/denied" not in str(err)
        # Still a store error for existing handlers, and a throttle still retries.
        assert isinstance(err, SecretStoreError)
        assert err.effective_retryable is True

    async def test_a_throttled_declared_ref_retries_not_blamed_on_the_user(
        self,
    ) -> None:
        """A throttle is the store saying "try again": retryable, and not a
        USER "fix your grants" error."""
        from application_sdk.infrastructure.secrets import SecretStoreTransientError

        class ThrottledStore:
            async def get(self, name: str) -> str:
                raise SecretStoreTransientError(
                    "ThrottlingException: Rate exceeded",
                    secret_name=name,
                    retryable=True,
                )

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql")})
        )
        with pytest.raises(SecretStoreTransientError) as exc_info:
            await resolve_agent_credential(spec, ThrottledStore())  # type: ignore[arg-type]
        err = exc_info.value
        assert not isinstance(err, AgentSecretReadError)
        assert err.effective_retryable is True
        assert "prod/mysql" not in str(err)
        assert "basic.password" in str(err)

    async def test_a_non_store_failure_stays_platform(self) -> None:
        """A bug in the fetch path is not the customer's to fix."""

        class BrokenStore:
            async def get(self, name: str) -> str:
                raise TypeError("unexpected payload shape")

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql")})
        )
        with pytest.raises(SecretStoreError) as exc_info:
            await resolve_agent_credential(spec, BrokenStore())  # type: ignore[arg-type]
        assert not isinstance(exc_info.value, AgentSecretReadError)
        assert exc_info.value.audience is not Audience.USER

    async def test_declared_ref_outage_keeps_its_type(self) -> None:
        class DownStore:
            async def get(self, name: str) -> str:
                raise SecretStoreUnavailableError(name)

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql")})
        )
        # An outage stays an outage (retryable), whichever end of the
        # cold-start budget it surfaces at.
        with pytest.raises((SecretStoreUnavailableError, SecretStoreUnreachableError)):
            await resolve_agent_credential(spec, DownStore())  # type: ignore[arg-type]


class TestPreflight:
    async def test_declared_payload_passes_the_secret_store_check(self) -> None:
        store = RecordingStore({"prod/mysql": "s3cret"})
        spec = AgentCredentialSpec.model_validate(
            _v2(
                **{
                    "basic.username": {"value": "u"},
                    "basic.password": _ref("prod/mysql"),
                }
            )
        )
        result = await check_secret_store_access(spec, store)  # type: ignore[arg-type]
        assert result.passed
        assert not result.fatal
        assert result.substituted == 1
        assert result.resolved is not None
        assert result.resolved["password"] == "s3cret"
        assert store.calls == ["prod/mysql"]

    async def test_all_literal_payload_passes_without_a_lookup(self) -> None:
        store = RecordingStore()
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.username": {"value": "u"}})
        )
        result = await check_secret_store_access(spec, store)  # type: ignore[arg-type]
        assert result.passed
        assert not result.fatal
        assert store.calls == []

    async def test_declared_ref_not_found_is_fatal_config_gap(self) -> None:
        class EmptyStore:
            async def get(self, name: str) -> str:
                raise SecretNotFoundError(name)

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/missing")})
        )
        result = await check_secret_store_access(spec, EmptyStore())  # type: ignore[arg-type]
        assert not result.passed
        assert result.fatal
        assert not result.store_down
        assert "basic.password" in result.message

    async def test_declared_ref_refused_is_a_config_gap_not_store_down(self) -> None:
        """A refused declared ref is the customer's grant to fix, so the row is
        a PRECONDITION config gap, not a store outage."""
        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/denied")})
        )
        result = await check_secret_store_access(spec, RecordingStore())  # type: ignore[arg-type]
        assert not result.passed
        assert result.fatal
        assert not result.store_down
        assert "basic.password" in result.message
        assert "prod/denied" not in result.message
        assert result.suggested_action

    async def test_declared_ref_outage_is_store_down(self) -> None:
        class DownStore:
            async def get(self, name: str) -> str:
                raise SecretStoreUnavailableError(name)

        spec = AgentCredentialSpec.model_validate(
            _v2(**{"basic.password": _ref("prod/mysql")})
        )
        result = await check_secret_store_access(spec, DownStore())  # type: ignore[arg-type]
        assert result.fatal
        assert result.store_down

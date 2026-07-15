"""Unit tests for the injected preflight-gate activity (``{app}:preflight``).

Separate from the SDR activity tests — the gate is its own module/concern.
"""

from __future__ import annotations

import warnings
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import AppContextError
from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.credentials.ref import CredentialResolvable
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    AuthError,
    ColdStartRaceError,
    DependencyUnavailableError,
)
from application_sdk.execution._temporal.preflight_gate import (
    GATE_START_TO_CLOSE,
    PreflightGateInput,
    _config_from_snapshot,
    build_preflight_gate_activity,
    input_type_supports_gate,
    preflight_gate_activity_name,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler, Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.infrastructure.credential_vault import CredentialVaultError

_UNSET = object()


class _StubHandler(Handler):
    """Records the preflight input it was called with."""

    def __init__(self) -> None:
        super().__init__()
        self.preflight_input: PreflightInput | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_input = input
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


def _gate(handler: Handler):
    activity = build_preflight_gate_activity(handler, app_name="myapp")
    assert getattr(activity, "__temporal_activity_definition").name == "myapp:preflight"
    return activity


class _VerdictHandler(DefaultHandler):
    """Returns a fixed PreflightOutput, to drive the gate's block decision."""

    def __init__(self, output: PreflightOutput) -> None:
        self._output = output

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return self._output


def _verdict_gate(output: PreflightOutput):
    return build_preflight_gate_activity(_VerdictHandler(output), app_name="myapp")


def _outcome_event(mock_logger) -> dict | None:
    """Return the kwargs of the single 'Preflight gate outcome' info call, if any."""
    for c in mock_logger.info.call_args_list:
        if c.args and c.args[0] == "Preflight gate outcome":
            return c.kwargs
    return None


_LOGGER = "application_sdk.execution._temporal.preflight_gate.logger"

_GATE = "application_sdk.execution._temporal.preflight_gate"


def _resolver_by_guid(mapping: dict) -> mock.MagicMock:
    """Resolver whose resolve_raw returns/raises per the ref's credential_guid.

    A dict value is returned as the raw creds; an Exception value is raised
    (drives the not-found / outage taxonomy branches).
    """

    def _resolve(ref):
        if ref.credential_guid not in mapping:
            raise KeyError(
                f"unexpected guid {ref.credential_guid!r} — declare it in the mapping"
            )
        result = mapping[ref.credential_guid]
        if isinstance(result, Exception):
            raise result
        return result

    resolver = mock.MagicMock()
    resolver.resolve_raw = mock.AsyncMock(side_effect=_resolve)
    return resolver


@contextmanager
def _infra_patches(resolver: mock.MagicMock | None, *, secret_store: Any = _UNSET):
    """Patch get_infrastructure (with the given secret_store) and CredentialResolver."""
    fake_infra = mock.MagicMock()
    fake_infra.secret_store = (
        mock.MagicMock(name="SecretStore") if secret_store is _UNSET else secret_store
    )
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch(f"{_GATE}.get_infrastructure", return_value=fake_infra)
        )
        if resolver is not None:
            stack.enter_context(
                mock.patch(f"{_GATE}.CredentialResolver", return_value=resolver)
            )
        yield


class TestPreflightGateActivity:
    def test_activity_name_is_app_namespaced(self) -> None:
        # Reads as a native workflow step ({app}:preflight), like the app's own
        # {app}:<task> activities — not a foreign sdr:/preflight: namespace.
        assert preflight_gate_activity_name("mysql") == "mysql:preflight"
        assert not preflight_gate_activity_name("mysql").startswith("sdr:")

    def test_gate_input_satisfies_credential_resolvable(self) -> None:
        assert isinstance(PreflightGateInput(), CredentialResolvable)

    async def test_gate_with_default_handler_proceeds(self) -> None:
        gate = _gate(DefaultHandler())
        result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY

    async def test_gate_resolves_guid_and_calls_handler_with_flattened_creds(
        self,
    ) -> None:
        handler = _StubHandler()
        gate = _gate(handler)

        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(
            return_value={"host": "db", "username": "u", "extra": {"role": "r"}}
        )
        with _infra_patches(resolver):
            result = await gate(
                PreflightGateInput(credential_guid="guid-123", entrypoint="crawl")
            )

        assert result.status is PreflightStatus.READY
        assert handler.preflight_input is not None
        seen = {c.key: c.value for c in handler.preflight_input.credentials}
        assert seen == {"host": "db", "username": "u", "extra.role": "r"}
        assert handler.preflight_input.entrypoint == "crawl"
        resolver.resolve_raw.assert_awaited_once()

    async def test_gate_without_routing_skips_resolution(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        with mock.patch(
            "application_sdk.execution._temporal.preflight_gate.CredentialResolver",
        ) as resolver_cls:
            await gate(PreflightGateInput())
        resolver_cls.assert_not_called()
        assert handler.preflight_input is not None
        assert handler.preflight_input.credentials == []

    async def test_gate_passes_enforced_per_attempt_timeout_budget(self) -> None:
        # timeout_seconds must carry the real per-attempt cap (start_to_close),
        # not the misleading 60s default — a handler sizing checks to the field
        # value would otherwise silently overrun and degrade to no_verdict.
        handler = _StubHandler()
        gate = _gate(handler)
        await gate(PreflightGateInput())
        assert handler.preflight_input is not None
        assert handler.preflight_input.timeout_seconds == int(
            GATE_START_TO_CLOSE.total_seconds()
        )
        # Legacy single-credential path leaves the named-group map empty.
        assert handler.preflight_input.credentials_by_name == {}

    async def test_raises_when_secret_store_unavailable(self) -> None:
        # A ref exists but there is no secret store to resolve it — an infra
        # failure. Raise (routes to the workflow's fail-open) rather than call the
        # handler with empty creds and misattribute the block as AUTH.
        handler = _StubHandler()
        gate = _gate(handler)
        with _infra_patches(None, secret_store=None):
            with pytest.raises(DependencyUnavailableError):
                await gate(PreflightGateInput(credential_guid="guid-123"))
        assert handler.preflight_input is None  # bailed before calling the handler

    async def test_gate_clears_context_after_call(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        await gate(PreflightGateInput())
        with pytest.raises(AppContextError):
            _ = handler.context

    async def test_ready_returns_without_raising(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        result = await _verdict_gate(out)(PreflightGateInput())
        assert result is out

    async def test_partial_returns_without_raising(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[PreflightCheck(name="version", passed=False, message="old")],
        )
        result = await _verdict_gate(out)(PreflightGateInput())
        assert result is out

    async def test_not_ready_raises_with_typed_primary_and_all_checks(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="conn", passed=True),
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(
                        message="Auth failed", suggested_action="Rotate the credential"
                    ),
                ),
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput(entrypoint="crawl"))
        err = excinfo.value
        assert err.type == "PreflightFailed"
        assert err.non_retryable is True
        details = err.details[0]
        assert details.category is FailureCategory.AUTH
        assert details.code == "AUTH"
        assert details.audience is Audience.USER
        assert details.message == "Auth failed"
        assert details.suggested_action == "Rotate the credential"
        # details[1] carries every check (a failed activity has no result payload).
        names = [c["name"] for c in err.details[1]["checks"]]
        assert names == ["conn", "auth"]
        assert "Auth failed" in err.message

    async def test_not_ready_without_error_falls_back_to_precondition(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        details = excinfo.value.details[0]
        assert details.category is FailureCategory.PRECONDITION
        assert "bad creds" in details.message

    async def test_output_message_seeds_reason_over_per_check_join(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            message="Summary: 3 of 5 checks failed",
            checks=[PreflightCheck(name="auth", passed=False, message="auth down")],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        assert (
            excinfo.value.message == "Preflight failed: Summary: 3 of 5 checks failed"
        )
        assert "auth down" not in excinfo.value.message

    async def test_reason_joins_failed_check_messages_via_precedence(self) -> None:
        # error.message wins for the first check; check.message for the second.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth", passed=False, error=AuthError(message="auth down")
                ),
                PreflightCheck(name="net", passed=False, message="host unreachable"),
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        msg = excinfo.value.message
        assert msg.startswith("Preflight failed: ")
        assert "auth down" in msg
        assert "host unreachable" in msg
        assert "; " in msg

    async def test_block_details_survive_data_converter_round_trip(self) -> None:
        # details[1] is a new payload: a plain dict of per-check dumps whose nested
        # error embeds enum-bearing FailureDetails. In production these cross the
        # Temporal boundary through pydantic_data_converter; encode→decode here
        # catches any raw model/enum that would only fail on a live worker.
        from temporalio.contrib.pydantic import pydantic_data_converter

        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="conn", passed=True),
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(
                        message="Auth failed", suggested_action="Rotate the credential"
                    ),
                ),
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())

        pc = pydantic_data_converter.payload_converter
        restored = pc.from_payloads(pc.to_payloads(excinfo.value.details))

        auth = next(c for c in restored[1]["checks"] if c["name"] == "auth")
        assert auth["error"]["category"] == FailureCategory.AUTH.value
        assert auth["error"]["code"] == "AUTH"
        assert auth["error"]["audience"] == Audience.USER.value
        assert auth["error"]["message"] == "Auth failed"
        assert auth["error"]["suggested_action"] == "Rotate the credential"

    async def test_block_stamps_app_name_when_handler_error_omits_it(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        assert excinfo.value.details[0].app_name == "myapp"

    async def test_block_preserves_handler_supplied_app_name(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(message="x", app_name="custom-app"),
                )
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        assert excinfo.value.details[0].app_name == "custom-app"

    async def test_error_on_passed_check_not_selected_as_primary(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="ok", passed=True, error=AuthError(message="ignored")
                ),
                PreflightCheck(
                    name="perm",
                    passed=False,
                    error=AppPermissionDeniedError(message="denied"),
                ),
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        details = excinfo.value.details[0]
        assert details.category is FailureCategory.PERMISSION
        assert details.message == "denied"

    def test_from_extraction_input_reads_routing_fields(self) -> None:
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = None
            credential_ref = None

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.credential_guid == "g-9"
        assert gate_input.entrypoint == "crawl"
        # No declared named refs -> empty mapping (legacy single-triple path).
        assert gate_input.credential_ref_fields == {}

    def test_from_extraction_input_reads_declared_credential_ref_fields(self) -> None:
        # A multi-credential app declares its named guid fields as a class
        # attribute; the gate carries them onto the envelope (secret-free).
        class _MultiInp:
            preflight_credential_refs = {
                "api": "api_credential_guid",
                "object_store": "object_store_credential_guid",
            }
            extraction_method = "direct"
            credential_guid = ""
            agent_json = None
            credential_ref = None

        gate_input = PreflightGateInput.from_extraction_input(_MultiInp(), "crawl")
        assert gate_input.credential_ref_fields == {
            "api": "api_credential_guid",
            "object_store": "object_store_credential_guid",
        }

    def test_from_extraction_input_degrades_on_unbuildable_metadata(self) -> None:
        # A custom input whose metadata can't fit the model must not raise —
        # the gate has to fail open before dispatch, not only during it.
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = None
            credential_ref = None
            metadata = 12345  # not a mapping / BaseMetadataConfig

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.entrypoint == "crawl"  # built, did not raise
        assert gate_input.credential_guid == "g-9"

    def test_from_extraction_input_stores_snapshot(self) -> None:
        # Gate now stores the raw model_dump() as extraction_snapshot so the
        # activity can build PreflightInput metadata in the activity frame, not
        # in the deterministic workflow context on replay.
        from pydantic import BaseModel

        class _Model(BaseModel):
            extraction_method: str = "direct"
            credential_guid: str = "g-9"
            agent_json: None = None
            credential_ref: None = None
            include_filter: dict = {}

            def model_dump(self, **kw):
                return {
                    "extraction_method": "direct",
                    "credential_guid": "g-9",
                    "agent_json": None,
                    "credential_ref": None,
                    "include_filter": {"^db$": ["^s$"]},
                }

        gate_input = PreflightGateInput.from_extraction_input(_Model(), "crawl")
        assert gate_input.extraction_snapshot.get("include_filter") == {"^db$": ["^s$"]}
        # Routing fields are still in the snapshot (excluded by _config_from_snapshot)
        assert "credential_guid" in gate_input.extraction_snapshot

    def test_from_extraction_input_snapshot_failure_degrades(self) -> None:
        # If model_dump() raises, snapshot is empty but the gate still builds.
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = None
            credential_ref = None

            def model_dump(self, **kw) -> dict:
                raise RuntimeError("dump failed")

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.entrypoint == "crawl"  # did not raise
        assert gate_input.extraction_snapshot == {}

    def test_from_extraction_input_degrades_on_pydantic_validation_failure(
        self,
    ) -> None:
        # An input field that won't fit PreflightGateInput (e.g. credential_ref
        # as a plain string rather than a CredentialRef) triggers ValidationError.
        # The gate must degrade to a minimal input, never raise.
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = None
            credential_ref = "not-a-CredentialRef"  # wrong type → ValidationError

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.entrypoint == "crawl"  # built, did not raise
        # Minimal fallback: routing fields from the bad input are not present
        assert gate_input.credential_guid == ""

    async def test_gate_mirrors_config_into_metadata_and_connection_config(
        self,
    ) -> None:
        # Handlers may read config from either metadata or connection_config; the
        # gate builds both from the snapshot, matching the HTTP /check path.
        handler = _StubHandler()
        gate = _gate(handler)

        await gate(
            PreflightGateInput(
                entrypoint="crawl",
                extraction_snapshot={"include-filter": {"^db$": ["^s$"]}},
            )
        )

        assert handler.preflight_input is not None
        assert handler.preflight_input.metadata.model_dump().get("include-filter") == {
            "^db$": ["^s$"]
        }
        assert handler.preflight_input.connection_config.model_dump().get(
            "include-filter"
        ) == {"^db$": ["^s$"]}

    def test_config_from_snapshot_excludes_routing_keys_and_adds_hyphen_variants(
        self,
    ) -> None:
        snapshot = {
            "extraction_method": "direct",
            "credential_guid": "g-9",
            "agent_json": None,
            "credential_ref": None,
            "include_filter": {"^db$": ["^s$"]},
            "connection_timeout": 30,
        }
        config = _config_from_snapshot(snapshot)
        # Routing keys must be absent
        for key in (
            "extraction_method",
            "credential_guid",
            "agent_json",
            "credential_ref",
        ):
            assert key not in config, f"Routing key {key!r} leaked into config"
        # Non-routing fields present with original and hyphenated names
        assert config.get("include_filter") == {"^db$": ["^s$"]}
        assert config.get("include-filter") == {"^db$": ["^s$"]}
        assert config.get("connection_timeout") == 30
        assert config.get("connection-timeout") == 30

    def test_config_from_snapshot_preserves_false_and_zero_drops_empties(self) -> None:
        snapshot = {
            "strict_mode": False,  # falsy but meaningful — must survive
            "retry_budget": 0,  # falsy but meaningful — must survive
            "temp_table_regex": "",  # genuinely empty — dropped
            "include_filter": {},  # genuinely empty — dropped
            "exclude_list": [],  # genuinely empty — dropped
            "scope": "public",  # truthy — survives
        }
        config = _config_from_snapshot(snapshot)
        assert config.get("strict_mode") is False
        assert config.get("strict-mode") is False
        assert config.get("retry_budget") == 0
        assert config.get("scope") == "public"
        for dropped in ("temp_table_regex", "include_filter", "exclude_list"):
            assert dropped not in config

    def test_config_from_snapshot_drops_named_credential_guid_fields(self) -> None:
        # Named-credential guid fields are refs, not form config — they must not
        # leak into metadata/connection_config the way the top-level triple can't.
        snapshot = {
            "api_credential_guid": "guid-a",
            "object_store_credential_guid": "guid-o",
            "scope": "public",
        }
        config = _config_from_snapshot(
            snapshot, ("api_credential_guid", "object_store_credential_guid")
        )
        assert "api_credential_guid" not in config
        assert "object_store_credential_guid" not in config
        assert config.get("scope") == "public"

    async def test_activity_uses_snapshot_to_build_preflight_metadata(self) -> None:
        # When extraction_snapshot is populated, the activity must derive metadata
        # from it (activity frame), not from input.metadata (workflow frame).
        handler = _StubHandler()
        gate = _gate(handler)

        await gate(
            PreflightGateInput(
                entrypoint="crawl",
                extraction_snapshot={
                    "extraction_method": "direct",
                    "credential_guid": "",
                    "include_filter": {"^db$": ["^s$"]},
                },
            )
        )

        assert handler.preflight_input is not None
        # include_filter (and its hyphenated variant) must appear via snapshot path
        assert handler.preflight_input.metadata.model_dump().get("include_filter") == {
            "^db$": ["^s$"]
        }
        assert handler.preflight_input.metadata.model_dump().get("include-filter") == {
            "^db$": ["^s$"]
        }


class TestPreflightGateOutcomeEvent:
    """The activity emits the queryable 'Preflight gate outcome' event (connector-pulse)."""

    async def test_proceeded_ready(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="auth", passed=True)],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput(entrypoint="crawl"))
        ev = _outcome_event(ml)
        assert ev is not None
        assert ev["outcome"] == "proceeded"
        assert ev["reason"] == "ready"
        assert ev["app_name"] == "myapp"
        assert ev["entrypoint"] == "crawl"
        assert ev["checks"] == 1
        # status/typed/error_type are collapsed into reason
        assert not ({"status", "typed", "error_type"} & ev.keys())

    async def test_proceeded_partial(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(name="auth", passed=True),
                PreflightCheck(name="tables", passed=False, message="advisory"),
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        ev = _outcome_event(ml)
        assert ev["outcome"] == "proceeded" and ev["reason"] == "partial"
        assert ev["entrypoint"] == "<implicit>"

    async def test_blocked_typed_reason_is_error_code(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        with mock.patch(_LOGGER) as ml, pytest.raises(ApplicationError):
            await _verdict_gate(out)(PreflightGateInput(entrypoint="crawl"))
        ev = _outcome_event(ml)
        assert ev["outcome"] == "blocked"
        # typed block → reason is the handler error's own code
        assert ev["reason"] == "AUTH"
        assert not ({"status", "typed", "error_type"} & ev.keys())

    async def test_blocked_fallback_reason_is_sentinel(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        with mock.patch(_LOGGER) as ml, pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        ev = _outcome_event(ml)
        assert ev["outcome"] == "blocked"
        # fallback block → reason is the sentinel code, distinguishing un-migrated
        assert ev["reason"] == "PREFLIGHT_CHECK_FAILED"
        # details[0] carries the sentinel code; category stays PRECONDITION
        details = excinfo.value.details[0]
        assert details.code == "PREFLIGHT_CHECK_FAILED"
        assert details.category is FailureCategory.PRECONDITION

    async def test_verdict_log_replaced_by_outcome_event(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        assert not any(
            c.args and "Preflight gate verdict" in str(c.args[0])
            for c in ml.info.call_args_list
        )
        assert _outcome_event(ml) is not None


class TestInputTypeSupportsGate:
    """Boot-time eligibility check — mirrors the runtime CredentialResolvable guard."""

    def test_extraction_input_is_eligible(self) -> None:
        from application_sdk.templates.contracts import ExtractionInput

        assert input_type_supports_gate(ExtractionInput) is True

    def test_bare_input_is_not_eligible(self) -> None:
        from application_sdk.contracts.base import Input

        assert input_type_supports_gate(Input) is False

    def test_input_missing_a_routing_field_is_not_eligible(self) -> None:
        from application_sdk.contracts.base import Input

        # Declares two of the three CredentialResolvable fields — not enough.
        class _Partial(Input):
            credential_guid: str = ""
            extraction_method: str = ""

        assert input_type_supports_gate(_Partial) is False

    def test_non_model_type_is_not_eligible(self) -> None:
        assert input_type_supports_gate(str) is False


class TestPreflightGateMultiCredential:
    """Named-credential resolution on the gate path (multi-credential apps).

    The gate resolves each declared guid and applies ONE fail-open taxonomy:
    a confirmed outage propagates (workflow fails open); a genuinely absent
    credential becomes an empty group so the handler decides.
    """

    _FIELDS = {
        "api": "api_credential_guid",
        "object_store": "object_store_credential_guid",
    }
    _SNAPSHOT = {
        "api_credential_guid": "guid-a",
        "object_store_credential_guid": "guid-o",
    }

    def _input(self, **overrides) -> PreflightGateInput:
        return PreflightGateInput(
            entrypoint="crawl",
            credential_ref_fields=self._FIELDS,
            extraction_snapshot={**self._SNAPSHOT, **overrides},
        )

    async def test_resolves_each_named_ref_into_its_own_group(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid(
            {"guid-a": {"token": "t"}, "guid-o": {"bucket": "b"}}
        )
        with _infra_patches(resolver):
            result = await gate(self._input())

        assert result.status is PreflightStatus.READY
        pi = handler.preflight_input
        assert pi is not None
        assert {c.key: c.value for c in pi.credentials_by_name["api"]} == {"token": "t"}
        assert {c.key: c.value for c in pi.credentials_by_name["object_store"]} == {
            "bucket": "b"
        }
        # Named path leaves the flat legacy list empty — handlers read the map.
        assert pi.credentials == []
        assert resolver.resolve_raw.await_count == 2

    async def test_not_found_group_is_empty_and_handler_still_runs(self) -> None:
        # A genuinely missing guid must not fail open and must not abort the gate;
        # the handler receives an empty group and decides the verdict itself.
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid(
            {"guid-a": {"token": "t"}, "guid-o": CredentialNotFoundError("guid-o")}
        )
        with _infra_patches(resolver):
            await gate(self._input())

        pi = handler.preflight_input
        assert pi is not None  # handler ran — not fail-open
        assert pi.credentials_by_name["object_store"] == []
        assert {c.key: c.value for c in pi.credentials_by_name["api"]} == {"token": "t"}

    async def test_outage_propagates_and_handler_is_not_called(self) -> None:
        # A confirmed dependency outage must propagate (→ workflow fail-open),
        # never be read as a bad credential and reach the handler.
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid(
            {"guid-a": DependencyUnavailableError(message="down", service="vault")}
        )
        with _infra_patches(resolver):
            with pytest.raises(DependencyUnavailableError):
                await gate(self._input())
        assert handler.preflight_input is None

    async def test_credential_vault_outage_propagates(self) -> None:
        # The only outage shape that escapes _resolve_by_guid is a
        # CredentialVaultError whose cause is a ColdStartRaceError (resolver.py):
        # it must propagate out of the activity, not be swallowed to an empty
        # group and reach the handler as if the credential were merely absent.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            outage = CredentialVaultError(
                "vault unreachable", cause=ColdStartRaceError(message="cold start")
            )
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid({"guid-a": outage, "guid-o": {"bucket": "b"}})
        with _infra_patches(resolver):
            with pytest.raises(CredentialVaultError):
                await gate(self._input())
        assert handler.preflight_input is None

    async def test_named_path_redacts_every_group_secret(self) -> None:
        # Redaction must be additive: bind_invocation_context receives the secret
        # from every named group, not just one source (pins the concatenation).
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid(
            {"guid-a": {"token": "t"}, "guid-o": {"bucket": "b"}}
        )
        with (
            _infra_patches(resolver),
            mock.patch(f"{_GATE}.bind_invocation_context") as bind,
        ):
            await gate(self._input())
        bound_values = {c.value for c in bind.call_args.args[1]}
        assert {"t", "b"} <= bound_values

    async def test_no_secret_store_raises_before_resolving(self) -> None:
        handler = _StubHandler()
        gate = _gate(handler)
        with _infra_patches(None, secret_store=None):
            with pytest.raises(DependencyUnavailableError):
                await gate(self._input())
        assert handler.preflight_input is None

    async def test_absent_guids_skip_resolution_with_empty_groups(self) -> None:
        # Fields declared but no guids in the snapshot (e.g. automation-trigger
        # empty metadata) — resolve nothing, hand the handler empty groups.
        handler = _StubHandler()
        gate = _gate(handler)
        with _infra_patches(None):
            await gate(
                PreflightGateInput(
                    credential_ref_fields=self._FIELDS,
                    extraction_snapshot={},
                )
            )
        pi = handler.preflight_input
        assert pi is not None
        assert pi.credentials_by_name == {"api": [], "object_store": []}

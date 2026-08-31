"""Unit tests for the injected preflight-gate activity (``{app}:preflight``).

Separate from the SDR activity tests — the gate is its own module/concern.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
    _LOG_ROW_IS_ONLY_CHANNEL,
    EMPTY_CHECK_MATRIX,
    FAILURE_AUDIENCE_KEY,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    PREFLIGHT_CHECK_EVENT,
    PreflightGateInput,
    PreflightSurface,
    _config_from_snapshot,
    build_preflight_gate_activity,
    emit_preflight_check_outcome,
    emit_preflight_crash_outcome,
    gate_heartbeat_timings,
    gate_timeouts,
    input_type_supports_gate,
    is_preflight_block,
    preflight_gate_activity_name,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler, Handler, HandlerError
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
from application_sdk.observability.logger_adaptor import (
    CHECK_MATRIX_KEY,
    GATE_MODE_KEY,
    PREFLIGHT_SURFACE_KEY,
)

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


def _verdict_gate(output: PreflightOutput, *, enforce: bool = True):
    return build_preflight_gate_activity(
        _VerdictHandler(output), app_name="myapp", enforce=enforce
    )


def _outcome_event(mock_logger) -> dict | None:
    """Return the kwargs of the single 'Preflight gate outcome' call, if any.

    Scans info, warning and error: the outcome row is the level carrier
    (FND-901) — blocks at error, advisory-failure proceeds at warning,
    healthy rows at info.
    """
    for c in [
        *mock_logger.info.call_args_list,
        *mock_logger.warning.call_args_list,
        *mock_logger.error.call_args_list,
    ]:
        if c.args and c.args[0] == "Preflight gate outcome":
            return c.kwargs
    return None


def _outcome_level(mock_logger) -> str | None:
    for level in ("info", "warning", "error"):
        for c in getattr(mock_logger, level).call_args_list:
            if c.args and c.args[0] == "Preflight gate outcome":
                return level
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
        # timeout_seconds must carry the budget the gate actually enforces, not
        # the misleading 60s contract default — a handler sizing checks to the
        # field value would otherwise overrun and degrade to no_verdict. It is
        # net of credential resolution, so assert the bound rather than equality
        # (see test_preflight_gate_classification for the deduction itself).
        handler = _StubHandler()
        gate = _gate(handler)
        await gate(PreflightGateInput())
        assert handler.preflight_input is not None
        assert (
            0
            < handler.preflight_input.timeout_seconds
            <= (GATE_TIMEOUT_DEFAULT_SECONDS)
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

    async def test_not_ready_prefers_aggregate_output_error(self) -> None:
        # A non-fatal row sits ahead of the real cause in ``checks``. The handler
        # pinned the real cause on ``result.error``, so the block's primary detail
        # must come from there — not the first failed check with an error.
        from application_sdk.errors.leaves import SourceUnavailableError

        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            error=SourceUnavailableError(
                message="Configured object store is not accessible.",
                suggested_action="Grant read and write access.",
            ),
            checks=[
                PreflightCheck(
                    name="secret",
                    passed=False,
                    error=AuthError(message="a soft secret-store row"),
                ),
                PreflightCheck(
                    name="objstore",
                    passed=False,
                    error=SourceUnavailableError(
                        message="Configured object store is not accessible."
                    ),
                ),
            ],
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _verdict_gate(out)(PreflightGateInput())
        details = excinfo.value.details[0]
        # From result.error (SOURCE_UNAVAILABLE), not the first check's AuthError.
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.suggested_action == "Grant read and write access."
        assert "Configured object store is not accessible." in excinfo.value.message

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

    def test_from_extraction_input_warns_when_refs_declared_as_field(self) -> None:
        # Misdeclaration guard: a multi-credential app that declares
        # preflight_credential_refs as a Pydantic *field* instead of a ClassVar
        # would silently fall back to the single-credential path (the class-level
        # read returns {}), blocking healthy runs. from_extraction_input must fall
        # back AND warn so the mistake is visible.
        from pydantic import BaseModel

        class _FieldDeclInput(BaseModel):
            preflight_credential_refs: dict[str, str] = {}

        with mock.patch(
            "application_sdk.execution._temporal.preflight_gate.logger"
        ) as mock_logger:
            gate_input = PreflightGateInput.from_extraction_input(
                _FieldDeclInput(), "crawl"
            )

        assert gate_input.credential_ref_fields == {}
        mock_logger.warning.assert_called_once()

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
        # The gate must degrade, never raise — and degrade *only* the rejected
        # field: dropping the routing triple with it makes the gate resolve the
        # wrong credential, or none, and report a fail-open verdict nobody can
        # trace back to the offending field.
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = None
            credential_ref = "not-a-CredentialRef"  # wrong type → ValidationError

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.entrypoint == "crawl"  # built, did not raise
        assert gate_input.credential_ref is None  # the rejected field, dropped
        assert gate_input.extraction_method == "direct"  # routing survives
        assert gate_input.credential_guid == "g-9"

    def test_from_extraction_input_keeps_routing_past_a_placeholder_agent_json(
        self,
    ) -> None:
        # The live shape of this bug: AE replays the marketplace-package
        # placeholder blob onto a direct-mode run. It is not an agent reference
        # (``port`` is the spec's only non-str field), and it used to fail the
        # whole gate input — silently costing the gate its extraction_method and
        # credential_guid, so credential resolution degraded with no report.
        class _Inp:
            extraction_method = "direct"
            credential_guid = "g-9"
            agent_json = {
                "agent-name": "agent-name",
                "host": "host",
                "port": "port",
                "secret-manager": "secret-manager",
            }
            credential_ref = None

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.agent_json is None  # placeholder is not a reference
        assert gate_input.extraction_method == "direct"
        assert gate_input.credential_guid == "g-9"

    def test_from_extraction_input_normalizes_a_serialized_agent_json(self) -> None:
        # A custom input may still carry the raw wire value; the gate's field is
        # typed, so ingress normalisation happens here rather than at each reader.
        class _Inp:
            extraction_method = "agent"
            credential_guid = ""
            agent_json = json.dumps(
                {"agent-name": "acme", "secret-path": "arn:x", "port": 1521}
            )
            credential_ref = None

        gate_input = PreflightGateInput.from_extraction_input(_Inp(), "crawl")
        assert gate_input.agent_json is not None
        assert gate_input.agent_json.agent_name == "acme"
        assert gate_input.agent_json.port == 1521

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
        # Advisory failure: WARNING is the one level semantically for it, and
        # P047 bans the handler from emitting it — so the gate must.
        assert _outcome_level(ml) == "warning"

    async def test_ready_with_failed_advisory_check_warns(self) -> None:
        # Keyed on the checks, not PreflightStatus.PARTIAL: PARTIAL is
        # documented display-only, so READY with a failed advisory row is the
        # same advisory case and must not silently flatten to INFO.
        out = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(name="auth", passed=True),
                PreflightCheck(name="tables", passed=False, message="advisory"),
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        assert _outcome_level(ml) == "warning"
        ml.error.assert_not_called()

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

    async def test_soft_not_ready_returns_and_emits_would_block(self) -> None:
        # Soft gate: the verdict stays honest NOT_READY, the gate just doesn't
        # enforce it — no raise, the run proceeds, and the dodged block is the
        # queryable would_block row connector-pulse ranks smelly apps by.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(message="x"),
                    duration_ms=312.0,
                )
            ],
        )
        with mock.patch(_LOGGER) as ml:
            result = await _verdict_gate(out, enforce=False)(PreflightGateInput())
        assert result is out
        assert result.status is PreflightStatus.NOT_READY  # verdict untouched
        ev = _outcome_event(ml)
        assert ev["outcome"] == "would_block"
        assert ev[GATE_MODE_KEY] == "soft"
        # reason still carries the primary failure's code, same as a hard block
        assert ev["reason"] == "AUTH"
        # Pin the full row on the would_block path too, mirroring the block path,
        # so an error_code/duration_ms field drop is caught here as well.
        assert json.loads(ev[CHECK_MATRIX_KEY]) == [
            {
                "name": "auth",
                "passed": False,
                "error_code": "AUTH",
                "duration_ms": 312.0,
            }
        ]

    async def test_hard_block_logs_at_error_with_user_audience(self) -> None:
        # FND-901: a block aborts the customer's run, and the customer-facing log
        # view filters at ERROR — so the outcome row itself is the ERROR record,
        # stamped with who must act (the primary check's typed audience).
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        with mock.patch(_LOGGER) as ml, pytest.raises(ApplicationError):
            await _verdict_gate(out)(PreflightGateInput())
        assert _outcome_level(ml) == "error"
        ev = _outcome_event(ml)
        assert ev[FAILURE_AUDIENCE_KEY] == "USER"
        # An expected typed outcome, not a crash: no stack on the verdict path.
        assert "exc_info" not in ev
        assert ml.error.call_count == 1

    async def test_soft_would_block_stays_info_with_audience(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out, enforce=False)(PreflightGateInput())
        assert _outcome_level(ml) == "info"
        assert _outcome_event(ml)[FAILURE_AUDIENCE_KEY] == "USER"
        ml.error.assert_not_called()

    async def test_proceeded_stays_info_without_audience(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        assert _outcome_level(ml) == "info"
        assert FAILURE_AUDIENCE_KEY not in _outcome_event(ml)
        ml.error.assert_not_called()

    async def test_hard_block_emits_gate_mode_hard(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        with mock.patch(_LOGGER) as ml, pytest.raises(ApplicationError):
            await _verdict_gate(out)(PreflightGateInput())
        ev = _outcome_event(ml)
        assert ev["outcome"] == "blocked"
        assert ev[GATE_MODE_KEY] == "hard"

    async def test_proceeded_carries_gate_mode_hard(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        assert _outcome_event(ml)[GATE_MODE_KEY] == "hard"

    async def test_proceeded_carries_gate_mode_soft(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out, enforce=False)(PreflightGateInput())
        ev = _outcome_event(ml)
        # a soft app's healthy runs proceed normally, still tagged soft
        assert ev["outcome"] == "proceeded"
        assert ev[GATE_MODE_KEY] == "soft"

    async def test_check_matrix_on_proceed(self) -> None:
        # The matrix is the pattern-analysis payload: small fixed fields only,
        # JSON-encoded so it lands as one LogAttributes value in ClickHouse.
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(message="x"),
                    duration_ms=312.0,
                ),
                PreflightCheck(name="tables", passed=True, duration_ms=95.0),
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        ev = _outcome_event(ml)
        assert isinstance(ev[CHECK_MATRIX_KEY], str)
        assert json.loads(ev[CHECK_MATRIX_KEY]) == [
            {
                "name": "auth",
                "passed": False,
                "error_code": "AUTH",
                "duration_ms": 312.0,
            },
            {
                "name": "tables",
                "passed": True,
                "error_code": "",
                "duration_ms": 95.0,
            },
        ]

    async def test_check_matrix_on_block(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(message="x"),
                    duration_ms=312.0,
                )
            ],
        )
        with mock.patch(_LOGGER) as ml, pytest.raises(ApplicationError):
            await _verdict_gate(out)(PreflightGateInput())
        matrix = json.loads(_outcome_event(ml)[CHECK_MATRIX_KEY])
        # Pin the full row on the block path too, so error_code/duration_ms drift
        # is caught here as well as on the proceed path.
        assert matrix == [
            {
                "name": "auth",
                "passed": False,
                "error_code": "AUTH",
                "duration_ms": 312.0,
            }
        ]

    async def test_check_matrix_implausible_duration_coerced_to_sentinel(self) -> None:
        # nan/inf (orjson would emit null) and negatives collapse to the -1.0
        # "not measured" sentinel so the ClickHouse row stays numeric and
        # garbage never reads as a real duration; never raised (a raise here
        # would fail the gate open and lose the whole event).
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(name="auth", passed=False, duration_ms=float("nan")),
                PreflightCheck(name="tables", passed=True, duration_ms=float("inf")),
                PreflightCheck(name="views", passed=True, duration_ms=-52.0),
                PreflightCheck(name="perms", passed=True),
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        matrix = json.loads(_outcome_event(ml)[CHECK_MATRIX_KEY])  # must parse
        assert [row["duration_ms"] for row in matrix] == [-1.0, -1.0, -1.0, -1.0]

    async def test_check_matrix_empty_checks(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        assert _outcome_event(ml)[CHECK_MATRIX_KEY] == "[]"

    async def test_check_matrix_carries_no_messages(self) -> None:
        # Messages/evidence stay in the Temporal activity result — the event row
        # must stay small and free of user-facing text.
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    message="human text",
                    error=AuthError(message="secret-adjacent detail"),
                )
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        (row,) = json.loads(_outcome_event(ml)[CHECK_MATRIX_KEY])
        assert set(row) == {"name", "passed", "error_code", "duration_ms"}

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
        # Fields declared but NO guids in the snapshot (e.g. automation-trigger
        # empty metadata) — resolve nothing, hand the handler empty groups, and
        # log at debug, not warning: all-absent is the benign no-credential path.
        handler = _StubHandler()
        gate = _gate(handler)
        with _infra_patches(None), mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(
                PreflightGateInput(
                    credential_ref_fields=self._FIELDS,
                    extraction_snapshot={},
                )
            )
        pi = handler.preflight_input
        assert pi is not None
        assert pi.credentials_by_name == {"api": [], "object_store": []}
        mock_logger.warning.assert_not_called()
        mock_logger.debug.assert_called_once()

    async def test_partial_missing_ref_warns_and_leaves_group_empty(self) -> None:
        # Some refs resolve and one guid field is absent — the likely-typo case:
        # warn (naming the missing field) and leave that group empty while the
        # resolved ref still populates. Fail-open behavior is unchanged.
        handler = _StubHandler()
        gate = _gate(handler)
        resolver = _resolver_by_guid({"guid-a": {"token": "t"}})
        with _infra_patches(resolver), mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(
                PreflightGateInput(
                    credential_ref_fields=self._FIELDS,
                    extraction_snapshot={"api_credential_guid": "guid-a"},
                )
            )

        pi = handler.preflight_input
        assert pi is not None
        assert {c.key: c.value for c in pi.credentials_by_name["api"]} == {"token": "t"}
        assert pi.credentials_by_name["object_store"] == []
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.kwargs["missing_refs"] == {
            "object_store": "object_store_credential_guid"
        }


class TestEmitPreflightCheckOutcome:
    """The interactive-surface sibling row (FND-901): one schema, per-surface levels."""

    def _emit(self, result: PreflightOutput, **kwargs) -> mock.MagicMock:
        log = mock.MagicMock()
        emit_preflight_check_outcome(log, "myapp", result, **kwargs)
        return log

    def test_ready_row_shape(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="auth", passed=True)],
        )
        log = self._emit(
            out, surface=PreflightSurface.HTTP, entrypoint="crawl", request_id="r-1"
        )
        log.info.assert_called_once()
        assert log.error.call_count == 0
        args, kwargs = log.info.call_args
        assert args[0] == PREFLIGHT_CHECK_EVENT
        assert kwargs["outcome"] == "ready"
        assert kwargs["reason"] == "ready"
        assert kwargs["app_name"] == "myapp"
        assert kwargs["entrypoint"] == "crawl"
        assert kwargs["checks"] == 1
        assert kwargs[PREFLIGHT_SURFACE_KEY] == "http"
        assert kwargs["request_id"] == "r-1"
        assert json.loads(kwargs[CHECK_MATRIX_KEY])[0]["name"] == "auth"
        assert FAILURE_AUDIENCE_KEY not in kwargs

    def test_sdr_not_ready_is_error_with_reason_and_audience(self) -> None:
        # SDR mirrors the gate: the failure surfaces through a workflow run log
        # read at the default ERROR filter, so the row must be the ERROR record.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        log = self._emit(out, surface=PreflightSurface.SDR)
        log.error.assert_called_once()
        log.info.assert_not_called()
        kwargs = log.error.call_args.kwargs
        assert kwargs["outcome"] == "not_ready"
        assert kwargs["reason"] == "AUTH"
        assert kwargs[FAILURE_AUDIENCE_KEY] == "USER"
        assert kwargs[PREFLIGHT_SURFACE_KEY] == "sdr"

    def test_http_not_ready_stays_info(self) -> None:
        # HTTP: the verdict IS the response body rendered in the setup form —
        # the log is not the delivery channel, so no level escalation.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        log = self._emit(out, surface=PreflightSurface.HTTP)
        log.info.assert_called_once()
        log.error.assert_not_called()

    def test_sdr_advisory_failure_warns(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(name="auth", passed=True),
                PreflightCheck(name="tables", passed=False, message="advisory"),
            ],
        )
        log = self._emit(out, surface=PreflightSurface.SDR)
        log.warning.assert_called_once()
        log.error.assert_not_called()

    def test_sdr_clean_stays_info(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        log = self._emit(out, surface=PreflightSurface.SDR)
        log.info.assert_called_once()
        log.error.assert_not_called()

    def test_aggregate_error_outranks_first_failed_check(self) -> None:
        # Mirrors _build_block_error: SDR inserts a non-fatal secret-store row
        # ahead of the real failure and pins the real one on result.error, so
        # first-failed must not steal the row's reason or audience.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            error=AuthError(message="real failure").to_failure_details(),
            checks=[
                PreflightCheck(
                    name="Secret store",
                    passed=False,
                    error=DependencyUnavailableError(
                        message="store degraded", service="secret_store"
                    ).to_failure_details(),
                ),
                PreflightCheck(name="auth", passed=False),
            ],
        )
        kwargs = self._emit(out, surface=PreflightSurface.SDR).error.call_args.kwargs
        assert kwargs["reason"] == "AUTH"
        assert kwargs[FAILURE_AUDIENCE_KEY] == "USER"

    def test_not_ready_untyped_reason_is_sentinel(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        kwargs = self._emit(out, surface=PreflightSurface.HTTP).info.call_args.kwargs
        assert kwargs["reason"] == "PREFLIGHT_CHECK_FAILED"
        assert FAILURE_AUDIENCE_KEY not in kwargs

    def test_partial_keeps_status_reason_but_stamps_audience(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(name="auth", passed=True),
                PreflightCheck(
                    name="tables", passed=False, error=AuthError(message="advisory")
                ),
            ],
        )
        kwargs = self._emit(out, surface=PreflightSurface.HTTP).info.call_args.kwargs
        assert kwargs["outcome"] == "partial"
        assert kwargs["reason"] == "partial"
        assert kwargs[FAILURE_AUDIENCE_KEY] == "USER"

    def test_defaults_omit_optional_attrs(self) -> None:
        out = PreflightOutput(status=PreflightStatus.READY, checks=[])
        kwargs = self._emit(out, surface=PreflightSurface.SDR).info.call_args.kwargs
        assert kwargs["entrypoint"] == "<implicit>"
        assert "request_id" not in kwargs

    def test_every_surface_has_a_level_policy(self) -> None:
        # The enforcing half of the enum: a new PreflightSurface member with no
        # entry in the policy table fails here. _log_row_is_only_channel
        # deliberately defaults a miss to loud instead of raising (losing the
        # row beats logging it loud), so nothing at runtime would otherwise
        # notice — and pyright only warns, since this repo sets
        # reportArgumentType = "warning".
        assert set(_LOG_ROW_IS_ONLY_CHANNEL) == set(PreflightSurface)

    @pytest.mark.parametrize("surface", list(PreflightSurface))
    def test_every_surface_emits_one_row_carrying_its_wire_string(
        self, surface: PreflightSurface
    ) -> None:
        # Parametrized over the enum so a newly added member is exercised
        # rather than only routed: exactly one row, and the attribute is the
        # plain wire string dashboards filter on, not the enum object.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="auth", passed=False, error=AuthError(message="x"))
            ],
        )
        log = self._emit(out, surface=surface)
        calls = log.info.call_count + log.warning.call_count + log.error.call_count
        assert calls == 1
        kwargs = (
            log.info.call_args or log.warning.call_args or log.error.call_args
        ).kwargs
        assert kwargs[PREFLIGHT_SURFACE_KEY] == surface.value
        assert type(kwargs[PREFLIGHT_SURFACE_KEY]) is str


class TestEmitPreflightCrashOutcome:
    """CONNECT-1170 gap 6: a handler crash still produces a crash-marked row."""

    def test_typed_error_crash_row_attributes(self) -> None:
        log = mock.MagicMock()
        exc = DependencyUnavailableError(message="x", service="warehouse")
        emit_preflight_crash_outcome(
            log,
            "myapp",
            exc,
            surface=PreflightSurface.HTTP,
            request_id="r-1",
        )
        log.error.assert_called_once()
        kwargs = log.error.call_args.kwargs
        assert log.error.call_args.args[0] == PREFLIGHT_CHECK_EVENT
        assert kwargs["outcome"] == "crashed"
        assert kwargs["reason"] == DependencyUnavailableError.code
        assert kwargs["checks"] == 0
        assert kwargs[CHECK_MATRIX_KEY] == EMPTY_CHECK_MATRIX
        assert kwargs[PREFLIGHT_SURFACE_KEY] == "http"
        assert kwargs[FAILURE_AUDIENCE_KEY] == DependencyUnavailableError.audience.value
        assert kwargs["request_id"] == "r-1"

    def test_client_fault_is_counted_but_not_a_crash(self) -> None:
        # A wrong password (AUTH -> 401) is the response working as designed,
        # not a handler crash; in the crash series it would let setup-form
        # typos dominate, but dropping it entirely would re-open the
        # denominator hole. So it gets its own outcome, at the surface's
        # level policy: INFO on HTTP (the response carries the failure),
        # ERROR on SDR (the row is the only channel).
        log = mock.MagicMock()
        emit_preflight_crash_outcome(
            log, "myapp", AuthError(message="x"), surface=PreflightSurface.HTTP
        )
        log.error.assert_not_called()
        row = log.info.call_args.kwargs
        assert row["outcome"] == "client_fault"
        assert row["reason"] == AuthError.code
        assert row[FAILURE_AUDIENCE_KEY] == AuthError.audience.value

        log = mock.MagicMock()
        emit_preflight_crash_outcome(
            log,
            "myapp",
            HandlerError("bad request", http_status=400),
            surface=PreflightSurface.SDR,
        )
        log.info.assert_not_called()
        assert log.error.call_args.kwargs["outcome"] == "client_fault"

    def test_untyped_error_crash_row_attributes(self) -> None:
        log = mock.MagicMock()
        exc = ValueError("boom")
        emit_preflight_crash_outcome(log, "myapp", exc, surface=PreflightSurface.SDR)
        log.error.assert_called_once()
        kwargs = log.error.call_args.kwargs
        assert kwargs["outcome"] == "crashed"
        assert kwargs["reason"] == "ValueError"
        assert kwargs[PREFLIGHT_SURFACE_KEY] == "sdr"
        assert FAILURE_AUDIENCE_KEY not in kwargs
        assert "request_id" not in kwargs


class TestOrphanedAttemptEmission:
    """CONNECT-1170 gap 1: an attempt Temporal already abandoned still emits.

    On a production run, attempt 1 was killed at ``start_to_close`` but kept
    running (no heartbeat, so cancellation is never delivered) and wrote its
    ``would_block`` verdict row seconds after the deadline; attempt 2 then
    wrote ``proceeded``. The documented dedupe key ``(workflow_run_id,
    outcome)`` keeps both rows, so a consumer records a block for a run that
    passed — corrupting the series hard mode is decided from.
    """

    async def test_attempt_past_its_deadline_emits_no_verdict_row(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=False)
        # activity.info() as the abandoned attempt sees it: its start_to_close
        # deadline passed minutes ago, so Temporal has stopped waiting and may
        # already be running the next attempt.
        dead_attempt = SimpleNamespace(
            attempt=1,
            start_to_close_timeout=timedelta(seconds=60),
            started_time=datetime.now(timezone.utc) - timedelta(seconds=300),
        )
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=dead_attempt),
            mock.patch(_LOGGER) as m,
        ):
            await gate(PreflightGateInput())
        assert _outcome_event(m) is None


class TestHeartbeatCleanupCannotOutrankTheVerdict:
    """CONNECT-1170 Round 1 Must fix 1: cleanup must never replace the verdict.

    A heartbeat task that ignores its stop event and swallows cancellation
    makes ``finally`` raise ``CancelledError`` — a ``BaseException`` that
    replaces the block the gate just decided on, turning a hard-mode block
    into a fail-open proceed.
    """

    @staticmethod
    async def _unstoppable_loop(**kwargs):
        while True:
            await asyncio.sleep(60)

    async def test_hard_mode_block_survives_a_stuck_heartbeat_task(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=True)
        with (
            mock.patch(
                "application_sdk.execution.heartbeat.auto_heartbeat_loop",
                self._unstoppable_loop,
            ),
            mock.patch(f"{_GATE}.gate_heartbeat_timings", return_value=(60.0, 0.01)),
        ):
            with pytest.raises(BaseException) as excinfo:
                await gate(PreflightGateInput())
        assert is_preflight_block(excinfo.value) is True
        assert not isinstance(excinfo.value, asyncio.CancelledError)

    async def test_soft_mode_ready_survives_a_stuck_heartbeat_task(self) -> None:
        gate = _verdict_gate(
            PreflightOutput(status=PreflightStatus.READY, checks=[]), enforce=False
        )
        with (
            mock.patch(
                "application_sdk.execution.heartbeat.auto_heartbeat_loop",
                self._unstoppable_loop,
            ),
            mock.patch(f"{_GATE}.gate_heartbeat_timings", return_value=(60.0, 0.01)),
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY


class TestLiveAttemptStillEmits:
    """CONNECT-1170 gap 1: the liveness guard must not over-suppress.

    A live attempt — one inside its start_to_close window — keeps its verdict
    row; suppression is only for attempts Temporal has already abandoned.
    """

    async def test_live_attempt_still_emits_verdict_row(self) -> None:
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=False)
        live_attempt = SimpleNamespace(
            attempt=1,
            start_to_close_timeout=timedelta(seconds=60),
            started_time=datetime.now(timezone.utc),
        )
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=live_attempt),
            mock.patch(_LOGGER) as m,
        ):
            await gate(PreflightGateInput())
        event = _outcome_event(m)
        assert event is not None
        assert event["outcome"] == "would_block"

    async def test_attempt_near_its_deadline_still_emits_verdict_row(self) -> None:
        # Production geometry: the gate emits ~5s before the deadline (the
        # headroom GATE_ACTIVITY_HEADROOM_SECONDS reserves). A guard that
        # suppresses there is the over-suppression that would actually bite.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=False)
        near_deadline = SimpleNamespace(
            attempt=1,
            start_to_close_timeout=timedelta(seconds=60),
            started_time=datetime.now(timezone.utc) - timedelta(seconds=55),
        )
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=near_deadline),
            mock.patch(_LOGGER) as m,
        ):
            await gate(PreflightGateInput())
        event = _outcome_event(m)
        assert event is not None
        assert event["outcome"] == "would_block"


class TestGateHeartbeat:
    """CONNECT-1170 gap 3: the activity heartbeats while the handler runs."""

    async def test_heartbeat_sent_while_handler_runs(self) -> None:
        beat = asyncio.Event()

        def _record_beat() -> None:
            beat.set()

        class _WaitingReadyHandler(DefaultHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                await beat.wait()
                return PreflightOutput(status=PreflightStatus.READY, checks=[])

        gate = build_preflight_gate_activity(
            _WaitingReadyHandler(), app_name="myapp", enforce=False, budget_seconds=2
        )
        with (
            mock.patch(f"{_GATE}.gate_heartbeat_timings", return_value=(60.0, 0.01)),
            mock.patch(f"{_GATE}.activity.heartbeat", side_effect=_record_beat),
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY

    async def test_min_budget_heartbeat_still_fits_inside_start_to_close(self) -> None:
        # The knobs are derived, not fixed: at the 5s budget floor a fixed 60s
        # timeout would be server-capped to start_to_close and never fire
        # first, silently disabling stall detection for short-budget apps.
        start_to_close, _ = gate_timeouts(5, 1)
        timeout, interval = gate_heartbeat_timings(start_to_close.total_seconds())
        assert timeout < start_to_close.total_seconds()
        assert 0 < interval < timeout

    async def test_default_budget_reproduces_the_sdk_wide_pair(self) -> None:
        start_to_close, _ = gate_timeouts(None, None)
        timeout, interval = gate_heartbeat_timings(start_to_close.total_seconds())
        assert (timeout, interval) == (60.0, 10.0)


class TestCancelledAttemptSuppression:
    """Cancellation is the primary liveness signal — no clocks involved."""

    async def test_cancelled_attempt_emits_no_verdict_row(self) -> None:
        # Temporal delivers cancellation in heartbeat responses; a cancelled
        # attempt is abandoned even while still inside its deadline window.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=False)
        inside_deadline = SimpleNamespace(
            attempt=1,
            start_to_close_timeout=timedelta(seconds=60),
            started_time=datetime.now(timezone.utc),
        )
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=inside_deadline),
            mock.patch(f"{_GATE}.activity.is_cancelled", return_value=True),
            mock.patch(_LOGGER) as m,
        ):
            await gate(PreflightGateInput())
        assert _outcome_event(m) is None

    async def test_naive_started_time_live_and_expired(self) -> None:
        # temporalio stamps aware datetimes today; the naive branch is the
        # compatibility path and must judge liveness the same way.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        for age_seconds, expect_row in ((0, True), (300, False)):
            gate = _verdict_gate(out, enforce=False)
            naive = SimpleNamespace(
                attempt=1,
                start_to_close_timeout=timedelta(seconds=60),
                started_time=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=age_seconds),
            )
            with (
                mock.patch(f"{_GATE}.activity.info", return_value=naive),
                mock.patch(_LOGGER) as m,
            ):
                await gate(PreflightGateInput())
            assert (_outcome_event(m) is not None) is expect_row

    async def test_incident_geometry_is_suppressed(self) -> None:
        # The row that motivated gap 1 landed 3.2s past its deadline. The
        # 2s skew grace must not wave it through — a 5s grace did, which is
        # why the grace is tighter than the 5s emit headroom.
        out = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="connectivity", passed=False)],
        )
        gate = _verdict_gate(out, enforce=False)
        incident = SimpleNamespace(
            attempt=1,
            start_to_close_timeout=timedelta(seconds=155),
            started_time=datetime.now(timezone.utc) - timedelta(seconds=158.2),
        )
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=incident),
            mock.patch(_LOGGER) as m,
        ):
            await gate(PreflightGateInput())
        assert _outcome_event(m) is None

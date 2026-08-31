"""Unit tests for the injected preflight-gate activity (``{app}:preflight``).

Separate from the SDR activity tests — the gate is its own module/concern.
"""

from __future__ import annotations

import json
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
    _LOG_ROW_IS_ONLY_CHANNEL,
    FAILURE_AUDIENCE_KEY,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    PREFLIGHT_CHECK_EVENT,
    PreflightGateInput,
    PreflightSurface,
    _config_from_snapshot,
    build_preflight_gate_activity,
    emit_preflight_check_outcome,
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


def _gate(handler: Handler, *, verify_storage: bool = False):
    activity = build_preflight_gate_activity(
        handler, app_name="myapp", verify_storage=verify_storage
    )
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

    async def test_check_matrix_nonfinite_duration_coerced(self) -> None:
        # orjson emits null for nan/inf; we normalize to 0.0 so the ClickHouse
        # row stays numeric, never raised (a raise here would fail the gate
        # open and lose the whole event).
        out = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[
                PreflightCheck(name="auth", passed=False, duration_ms=float("nan")),
                PreflightCheck(name="tables", passed=True, duration_ms=float("inf")),
            ],
        )
        with mock.patch(_LOGGER) as ml:
            await _verdict_gate(out)(PreflightGateInput())
        matrix = json.loads(_outcome_event(ml)[CHECK_MATRIX_KEY])  # must parse
        assert [row["duration_ms"] for row in matrix] == [0.0, 0.0]

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


class TestPreflightGateStorageChecks:
    """The per-app opt-in storage probe folded into the gate verdict."""

    @staticmethod
    def _reloc_result():
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        return ObjectStoreCheckResult(
            label="deployment",
            binding_name="objectstore",
            passed=False,
            error_class="bucket relocation in progress",
            cause="400 PreconditionFailed: bucket relocation",
            hint="retry after the relocation finishes",
            failed_operation="write-multipart",
        )

    @staticmethod
    def _passed_result():
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        return ObjectStoreCheckResult(
            label="deployment", binding_name="objectstore", passed=True
        )

    @staticmethod
    @contextmanager
    def _storage_patches(probe_results, *, sdr_mode: bool = False):
        import application_sdk.constants as constants_mod

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(constants_mod, "ENABLE_ATLAN_UPLOAD", sdr_mode)
            )
            stack.enter_context(
                mock.patch(f"{_GATE}.get_infrastructure", return_value=mock.MagicMock())
            )
            checker = stack.enter_context(
                mock.patch(
                    "application_sdk.storage.preflight.check_run_storage_access",
                    new=mock.AsyncMock(return_value=probe_results),
                )
            )
            yield checker

    async def test_storage_checks_off_by_default(self) -> None:
        """Without the opt-in, the gate never touches the storage checker."""
        gate = _gate(_StubHandler())
        with self._storage_patches([self._reloc_result()]) as checker:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        checker.assert_not_awaited()

    async def test_relocation_blocks_hard_gate_with_typed_code(self) -> None:
        """A failed probe downgrades READY and blocks in hard mode, platform-attributed."""
        gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", enforce=True, verify_storage=True
        )
        with self._storage_patches([self._reloc_result()]):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.type == "PreflightFailed"
        details = excinfo.value.details[0]
        assert details.code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"
        assert details.audience is Audience.PLATFORM

    async def test_relocation_soft_gate_reports_not_ready(self) -> None:
        """Soft mode: verdict honestly NOT_READY, run proceeds (no raise)."""
        gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", enforce=False, verify_storage=True
        )
        with self._storage_patches([self._reloc_result()]):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        names = [c.name for c in result.checks]
        assert "objectStoreAccess:deployment" in names
        failed = next(c for c in result.checks if not c.passed)
        assert failed.error is not None
        assert failed.error.code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"

    async def test_healthy_storage_appends_passed_check(self) -> None:
        gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", enforce=True, verify_storage=True
        )
        with self._storage_patches([self._passed_result()]):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        check = next(
            c for c in result.checks if c.name == "objectStoreAccess:deployment"
        )
        assert check.passed is True

    async def test_storage_checks_skipped_when_handler_already_not_ready(self) -> None:
        """A blocked run is blocked; no storage probe spends more of the slot."""
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="denied")],
        )
        gate = build_preflight_gate_activity(
            _VerdictHandler(verdict),
            app_name="myapp",
            enforce=False,
            verify_storage=True,
        )
        with self._storage_patches([self._passed_result()]) as checker:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        checker.assert_not_awaited()

    async def test_storage_checker_crash_fails_open(self) -> None:
        """An unexpected checker failure must never eat the handler's verdict."""
        import application_sdk.constants as constants_mod

        gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", enforce=True, verify_storage=True
        )
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(constants_mod, "ENABLE_ATLAN_UPLOAD", False)
            )
            stack.enter_context(
                mock.patch(f"{_GATE}.get_infrastructure", return_value=mock.MagicMock())
            )
            stack.enter_context(
                mock.patch(
                    "application_sdk.storage.preflight.check_run_storage_access",
                    new=mock.AsyncMock(side_effect=RuntimeError("boom")),
                )
            )
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY

    async def test_storage_checks_skipped_when_budget_exhausted(self) -> None:
        """Below the time floor the probe is skipped — storage stays unverified."""
        gate = build_preflight_gate_activity(
            _StubHandler(),
            app_name="myapp",
            enforce=True,
            budget_seconds=5,
            verify_storage=True,
        )
        with (
            self._storage_patches([self._reloc_result()]) as checker,
            mock.patch(f"{_GATE}._STORAGE_CHECK_MIN_SECONDS", 10_000.0),
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        checker.assert_not_awaited()

    async def test_sdr_mode_uses_role_aware_mapping(self) -> None:
        """In SDR mode the deployment store maps via the SDR role split (USER)."""
        gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", enforce=False, verify_storage=True
        )
        with self._storage_patches([self._reloc_result()], sdr_mode=True):
            result = await gate(PreflightGateInput())
        failed = next(c for c in result.checks if not c.passed)
        assert failed.error is not None
        assert failed.error.code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"
        # Role-aware: an SDR deployment store is the customer's own bucket.
        assert failed.error.audience is Audience.USER

    async def test_storage_failure_on_non_final_attempt_retries(self) -> None:
        """A failed probe defers to the gate's retry policy before blocking.

        One flaky probe must not abort a hard-mode run on the first attempt —
        the block is only a verdict once the app's declared attempts are
        exhausted (mirrors the handler no-verdict path).
        """
        from datetime import timedelta

        from application_sdk.execution._temporal.preflight_gate import (
            PREFLIGHT_NO_VERDICT_ERROR_TYPE,
        )

        gate = build_preflight_gate_activity(
            _StubHandler(),
            app_name="myapp",
            enforce=True,
            attempts=2,
            verify_storage=True,
        )
        info = mock.MagicMock()
        info.attempt = 1
        info.start_to_close_timeout = timedelta(seconds=155)
        with (
            self._storage_patches([self._reloc_result()]),
            mock.patch(f"{_GATE}.activity.info", return_value=info),
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_NO_VERDICT_ERROR_TYPE
        assert excinfo.value.non_retryable is False

    async def test_storage_failure_on_final_attempt_blocks(self) -> None:
        """Retries exhausted → the storage failure becomes the blocking verdict."""
        from datetime import timedelta

        gate = build_preflight_gate_activity(
            _StubHandler(),
            app_name="myapp",
            enforce=True,
            attempts=2,
            verify_storage=True,
        )
        info = mock.MagicMock()
        info.attempt = 2
        info.start_to_close_timeout = timedelta(seconds=155)
        with (
            self._storage_patches([self._reloc_result()]),
            mock.patch(f"{_GATE}.activity.info", return_value=info),
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.type == "PreflightFailed"
        assert (
            excinfo.value.details[0].code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"
        )

    def test_every_classifier_bucket_maps_to_a_typed_leaf(self) -> None:
        """The gate mapper covers every bucket the storage classifier can emit.

        Iterates the classifier's own enumerable bucket set (the coupling the
        storage module documents for exactly this purpose) so a rule added or
        renamed there without gate handling fails here instead of silently
        taking a default branch. Also pins the relocation stamp to the
        *imported* bucket constant — the gate holds no copied literal.
        """
        from application_sdk.execution._temporal.preflight_gate import (
            _storage_failure_details,
        )
        from application_sdk.storage.errors import StorageBucketRelocationError
        from application_sdk.storage.preflight import (
            _OBJECT_STORE_ERROR_CLASSES,
            RELOCATION_BUCKET,
            ObjectStoreCheckResult,
        )

        OBJECT_STORE_RELOCATION_CODE = StorageBucketRelocationError.code

        assert RELOCATION_BUCKET in _OBJECT_STORE_ERROR_CLASSES
        for label in ("deployment", "upstream"):
            for bucket in _OBJECT_STORE_ERROR_CLASSES:
                probe = ObjectStoreCheckResult(
                    label=label,
                    binding_name="objectstore",
                    passed=False,
                    error_class=bucket,
                    cause="probe failed",
                    hint="fix it",
                    failed_operation="write-multipart",
                )
                for sdr_mode in (False, True):
                    details = _storage_failure_details(probe, sdr_mode=sdr_mode)
                    assert details.code, (label, bucket, sdr_mode)
                    assert details.audience is not None, (label, bucket, sdr_mode)
                    if bucket == RELOCATION_BUCKET:
                        assert details.code == OBJECT_STORE_RELOCATION_CODE

    async def test_relocation_code_not_shadowed_by_failed_advisory_check(self) -> None:
        """Reviewer repro: READY-with-failed-advisory must not steal the banner.

        ``_build_block_error`` prefers ``result.error`` over the first failed
        check's error; the storage downgrade pins ``result.error`` so the block
        is attributed to storage, not to the handler's advisory row.
        """
        import time as _time

        from application_sdk.errors.leaves import PreconditionError
        from application_sdk.execution._temporal.preflight_gate import (
            _append_storage_checks,
            _build_block_error,
        )
        from application_sdk.handler.contracts import PreflightOutput

        advisory = PreflightCheck(
            name="sourceAdvisory",
            passed=False,
            message="advisory",
            error=PreconditionError(message="advisory").to_failure_details(),
        )
        result = PreflightOutput(status=PreflightStatus.READY, checks=[advisory])
        with self._storage_patches([self._reloc_result()]):
            failed = await _append_storage_checks(result, 150.0, _time.monotonic())
        assert failed is True
        assert result.status is PreflightStatus.NOT_READY
        block = _build_block_error(result, "myapp")
        assert block.details[0].code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"

    async def test_partial_verdict_is_downgraded(self) -> None:
        """Reviewer repro: PARTIAL proceeds today, so it must downgrade too."""
        import time as _time

        from application_sdk.execution._temporal.preflight_gate import (
            _append_storage_checks,
        )
        from application_sdk.handler.contracts import PreflightOutput

        result = PreflightOutput(status=PreflightStatus.PARTIAL, checks=[])
        with self._storage_patches([self._reloc_result()]):
            failed = await _append_storage_checks(result, 150.0, _time.monotonic())
        assert failed is True
        assert result.status is PreflightStatus.NOT_READY

    async def test_handler_budget_reserves_storage_floor(self) -> None:
        """Opting in must reserve the storage floor out of the advertised budget.

        A handler that sizes its probes to ``PreflightInput.timeout_seconds`` —
        exactly what the docs tell it to do — must still leave the storage
        check its floor, or the opt-in silently degrades to a no-op skip.
        """
        from application_sdk.execution._temporal.preflight_gate import (
            _STORAGE_CHECK_MIN_SECONDS,
            GATE_TIMEOUT_DEFAULT_SECONDS,
        )

        handler = _StubHandler()
        gate = build_preflight_gate_activity(
            handler, app_name="myapp", verify_storage=True
        )
        with self._storage_patches([self._passed_result()]):
            await gate(PreflightGateInput())
        assert handler.preflight_input is not None
        assert (
            handler.preflight_input.timeout_seconds
            <= GATE_TIMEOUT_DEFAULT_SECONDS - _STORAGE_CHECK_MIN_SECONDS
        )

    async def test_budget_skip_appends_visible_row(self) -> None:
        """A budget-starved skip must be visible, not a debug-level nothing.

        An app owner who opted in (and connector-pulse) must be able to tell
        'storage verified clean' from 'storage never probed'.
        """
        gate = build_preflight_gate_activity(
            _StubHandler(),
            app_name="myapp",
            enforce=True,
            budget_seconds=5,
            verify_storage=True,
        )
        with (
            self._storage_patches([self._reloc_result()]) as checker,
            mock.patch(f"{_GATE}._STORAGE_CHECK_MIN_SECONDS", 10_000.0),
        ):
            result = await gate(PreflightGateInput())
        checker.assert_not_awaited()
        assert result.status is PreflightStatus.READY
        skipped = [c for c in result.checks if c.name == "objectStoreAccess:skipped"]
        assert len(skipped) == 1
        assert skipped[0].passed is False
        assert "not verified" in skipped[0].message

    async def test_error_pin_overwrites_handler_partial_error(self) -> None:
        """The pin must be unconditional: the storage failure IS the reason the
        verdict became NOT_READY, even when the handler set an aggregate error
        on its PARTIAL verdict."""
        import time as _time

        from application_sdk.errors.leaves import PreconditionError
        from application_sdk.execution._temporal.preflight_gate import (
            _append_storage_checks,
            _build_block_error,
        )
        from application_sdk.handler.contracts import PreflightOutput

        result = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            checks=[],
            error=PreconditionError(message="partial advisory").to_failure_details(),
        )
        with self._storage_patches([self._reloc_result()]):
            await _append_storage_checks(result, 150.0, _time.monotonic())
        assert result.status is PreflightStatus.NOT_READY
        from application_sdk.storage.errors import StorageBucketRelocationError

        block = _build_block_error(result, "myapp")
        assert block.details[0].code == StorageBucketRelocationError.code

    @pytest.mark.parametrize("declared", [5, 10, 16, 20, 30, 150, 300])
    async def test_reserve_never_starves_the_handler(self, declared: int) -> None:
        """The storage reserve must never take the handler below half its budget.

        An uncapped reserve hands a small-budget app's source handler a single
        second and then attributes the timeout to the source — the failure the
        credential-resolution guard exists to prevent, reached through the
        opt-in instead. Half is the same bound ``_min_handler_seconds`` uses.
        """
        from application_sdk.execution._temporal.preflight_gate import (
            _effective_budget,
            resolve_gate_budget_seconds,
        )

        budget = _effective_budget(resolve_gate_budget_seconds(declared))
        handler = _StubHandler()
        gate = build_preflight_gate_activity(
            handler,
            app_name="myapp",
            budget_seconds=resolve_gate_budget_seconds(declared),
            verify_storage=True,
        )
        with self._storage_patches([self._passed_result()]):
            await gate(PreflightGateInput())
        assert handler.preflight_input is not None
        # Half the budget, less the second the advertised value loses to
        # ``int()`` truncation and credential-resolution elapsed time. The bug
        # this pins gave a 10s-budget app's handler 1s, not 4s.
        assert handler.preflight_input.timeout_seconds >= budget / 2 - 1, (
            declared,
            handler.preflight_input.timeout_seconds,
        )

    async def test_unverified_row_is_distinguishable_from_a_probe_failure(
        self,
    ) -> None:
        """A skip and a real outage must not be the same row to a consumer.

        Both are failed ``objectStoreAccess:*`` checks, so the only thing that
        separates "never measured" from "the store rejected the write" is the
        typed code — and connector-pulse counts these.
        """
        from application_sdk.execution._temporal.preflight_gate import (
            OBJECT_STORE_UNVERIFIED_CODE,
        )
        from application_sdk.storage.errors import StorageBucketRelocationError

        skipped_gate = build_preflight_gate_activity(
            _StubHandler(), app_name="myapp", budget_seconds=5, verify_storage=True
        )
        with (
            self._storage_patches([self._reloc_result()]),
            mock.patch(f"{_GATE}._STORAGE_CHECK_MIN_SECONDS", 10_000.0),
        ):
            skipped = await skipped_gate(PreflightGateInput())
        (skip_row,) = [c for c in skipped.checks if c.name.startswith("objectStore")]
        assert skip_row.error is not None
        assert skip_row.error.code == OBJECT_STORE_UNVERIFIED_CODE
        assert skip_row.error.code != StorageBucketRelocationError.code

    async def test_absent_infrastructure_context_is_visible(self) -> None:
        """No store to probe is 'unverified', not an empty clean matrix."""
        from application_sdk.execution._temporal.preflight_gate import (
            OBJECT_STORE_UNVERIFIED_CODE,
        )

        gate = _gate(_StubHandler(), verify_storage=True)
        with self._storage_patches([]):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        (row,) = [c for c in result.checks if c.name.startswith("objectStore")]
        assert row.passed is False
        assert row.error is not None
        assert row.error.code == OBJECT_STORE_UNVERIFIED_CODE

    async def test_checker_crash_leaves_a_visible_row(self) -> None:
        """The fail-open must fail open on the verdict, not on the record."""
        from application_sdk.execution._temporal.preflight_gate import (
            OBJECT_STORE_UNVERIFIED_CODE,
        )

        gate = _gate(_StubHandler(), verify_storage=True)
        with mock.patch(
            f"{_GATE}.get_infrastructure", side_effect=RuntimeError("boom")
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        (row,) = [c for c in result.checks if c.name.startswith("objectStore")]
        assert row.passed is False
        assert row.error is not None
        assert row.error.code == OBJECT_STORE_UNVERIFIED_CODE


def test_gate_module_import_does_not_load_obstore() -> None:
    """Importing the gate module must not pull obstore into its import set.

    The gate is imported inside the Temporal workflow sandbox
    (``workflow.unsafe.imports_passed_through()``); every storage import in the
    module is deliberately lazy so the heavy obstore extension (and
    ``storage.ops``, which imports it at module load) only loads in the
    activity frame. A fresh interpreter proves it — this suite's own imports
    would mask the leak in-process.
    """
    import subprocess
    import sys

    code = (
        "import sys; "
        "import application_sdk.execution._temporal.preflight_gate; "
        "assert 'obstore' not in sys.modules, 'obstore leaked'; "
        "assert 'application_sdk.storage.ops' not in sys.modules, 'ops leaked'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

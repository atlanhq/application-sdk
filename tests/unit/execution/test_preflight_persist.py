"""The gate's store row: what it carries, and that writing it can never fail a run."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from pydantic import Field

from application_sdk import constants
from application_sdk.contracts.base import Input
from application_sdk.contracts.types import ConnectionRef
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.errors.wire import FailureDetails
from application_sdk.execution._temporal import preflight_persist as persist
from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    _config_from_snapshot,
)
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)

SLUG = "example-source-aBcD1234"
CONNECTION_QN = "default/example-source/1700000000"


class _ExtractionInput(Input):
    """A minimal gate-eligible input, shaped and typed like a connector's.

    Using the real field types means this exercises the same validation path a
    connector's own input does, rather than a looser one that would accept a
    shape the gate never sees.
    """

    extraction_method: str = ""
    credential_guid: str = ""
    agent_json: AgentCredentialSpec | None = None
    connection: ConnectionRef = Field(default_factory=ConnectionRef)


def _gate(**overrides) -> PreflightGateInput:
    return PreflightGateInput(entrypoint="extract-metadata", **overrides)


def _verdict(message: str = "all good") -> PreflightOutput:
    return PreflightOutput(status=PreflightStatus.READY, message=message)


class TestTheSlugReachesTheGate:
    def test_an_ae_arg_survives_the_input_model(self):
        # Input declares no extras, so an undeclared workflow_slug is dropped
        # before any reader sees it.
        assert _ExtractionInput(workflow_slug=SLUG).workflow_slug == SLUG

    def test_the_envelope_copies_it_off_the_input(self):
        gate = PreflightGateInput.from_extraction_input(
            _ExtractionInput(workflow_slug=SLUG), "extract-metadata"
        )
        assert gate.workflow_slug == SLUG

    def test_an_input_without_one_leaves_it_empty(self):
        gate = PreflightGateInput.from_extraction_input(
            _ExtractionInput(), "extract-metadata"
        )
        assert gate.workflow_slug == ""

    def test_the_reader_does_not_depend_on_the_snapshot_carrying_it(self):
        # The row builder reads the envelope's own field, so a snapshot that
        # never carried the slug still produces an attributed row.
        gate = PreflightGateInput(entrypoint="crawl", workflow_slug=SLUG)
        assert gate.extraction_snapshot == {}
        row = persist.build_check_result(gate, _verdict())
        assert row is not None and row.workflow_slug == SLUG

    def test_it_reaches_form_config_like_every_other_platform_field(self):
        # Not excluded, and deliberately so: _ROUTING_KEYS means *credential*
        # routing, and workflow_id / correlation_id / app_name are not dropped
        # either. Singling this one out would be an inconsistency, not a fix.
        config = _config_from_snapshot(
            _ExtractionInput(
                workflow_slug=SLUG,
                workflow_id="wf-1",
                correlation_id="corr-1",
                app_name="example-source",
            ).model_dump(mode="json")
        )
        for field in ("workflow_id", "correlation_id", "app_name", "workflow_slug"):
            assert field in config

    def test_it_does_not_move_a_checkpoint_key(self):
        without = _ExtractionInput().config_hash()
        assert _ExtractionInput(workflow_slug=SLUG).config_hash() == without


class TestTheRow:
    def test_no_slug_is_a_skip_and_not_a_failure(self):
        assert persist.build_check_result(_gate(), _verdict()) is None

    def test_a_whitespace_slug_counts_as_absent(self):
        assert persist.build_check_result(_gate(workflow_slug="  "), _verdict()) is None

    def test_it_declares_itself_as_the_activity_origin(self):
        row = persist.build_check_result(_gate(workflow_slug=SLUG), _verdict())
        assert row is not None
        assert row.origin == persist.PreflightResultOrigin.ACTIVITY

    def test_the_verdict_travels_inside_the_preflight_envelope(self):
        row = persist.build_check_result(_gate(workflow_slug=SLUG), _verdict())
        assert row is not None
        assert list(row.payload) == ["preflight"]
        assert row.payload["preflight"]["status"] == "ready"

    def test_an_unstamped_identity_is_absent_rather_than_empty(self):
        row = persist.build_check_result(
            _gate(workflow_slug=SLUG), _verdict(), app_id="", app_version=""
        )
        assert row is not None
        assert row.app_id is None
        assert row.app_version is None

    def test_a_stamped_identity_is_carried(self):
        row = persist.build_check_result(
            _gate(workflow_slug=SLUG), _verdict(), app_id="app-1", app_version="1.4.2"
        )
        assert row is not None
        assert (row.app_id, row.app_version) == ("app-1", "1.4.2")

    def test_an_absent_identity_is_left_out_of_the_body(self):
        row = persist.build_check_result(_gate(workflow_slug=SLUG), _verdict())
        assert row is not None
        assert "app_id" not in row.model_dump_json(exclude_none=True)


class TestWhatTheRowSaysAboutTheSource:
    @pytest.mark.parametrize(
        "gate_kwargs",
        [
            {"extraction_method": "agent"},
            {"extraction_method": "AGENT"},
            {"extraction_snapshot": {"agent-name": "customer-agent"}},
        ],
        ids=["declared", "declared-uppercase", "spec-only"],
    )
    def test_an_agent_is_recognised_from_either_signal(self, gate_kwargs):
        gate = _gate(workflow_slug=SLUG, **gate_kwargs)
        assert persist.extraction_method(gate) == persist.ExtractionMethod.AGENT

    @pytest.mark.parametrize("declared", ["", "direct", "s3", "offline"])
    def test_everything_else_is_direct(self, declared):
        gate = _gate(workflow_slug=SLUG, extraction_method=declared)
        assert persist.extraction_method(gate) == persist.ExtractionMethod.DIRECT

    @pytest.mark.parametrize(
        "snapshot",
        [
            {"connection": {"attributes": {"qualifiedName": CONNECTION_QN}}},
            {"connection": {"attributes": {"qualified_name": CONNECTION_QN}}},
            {"connection_qualified_name": CONNECTION_QN},
            {"connection-qualified-name": [CONNECTION_QN]},
        ],
        ids=["asset-camel", "asset-snake", "bare-string", "bare-list"],
    )
    def test_the_connection_is_read_from_every_shape_ae_sends(self, snapshot):
        assert persist.connection_qualified_name(snapshot) == CONNECTION_QN

    def test_a_workflow_with_no_connection_says_so(self):
        assert persist.connection_qualified_name({}) is None


class TestThePayloadMatchesWhatTheOtherWriterSends:
    """The ``preflight`` block is the contract between every writer and the store."""

    def test_a_failed_checks_typed_error_wins_over_its_deprecated_message(self):
        # What the frontend path records, so both writers put the same string in
        # the same place rather than leaving the store to re-derive it.
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    message="stale deprecated text",
                    error=AuthError(message="login refused").to_failure_details(),
                )
            ],
        )
        block = persist.verdict_payload(verdict)["preflight"]
        assert block["checks"][0]["message"] == "login refused"

    def test_the_aggregate_verdict_fields_are_carried(self):
        verdict = PreflightOutput(
            status=PreflightStatus.PARTIAL,
            message="two of three",
            total_duration_ms=12.5,
        )
        block = persist.verdict_payload(verdict)["preflight"]
        assert block["status"] == "partial"
        assert block["message"] == "two of three"
        assert block["total_duration_ms"] == 12.5

    def test_the_frontends_display_envelope_is_not_synthesised(self):
        # /sage wraps this block in success/message/data for the setup widget.
        # The store reads none of it, and building it here would be a second
        # implementation of heracles' envelope.
        payload = persist.verdict_payload(_verdict())
        assert list(payload) == ["preflight"]


class TestNothingSecretReachesTheStore:
    """Redaction here is load-bearing, not defence in depth.

    ``FailureDetails`` redacts only ``cause_repr``, and only where it is built.
    ``message``, ``suggested_action`` and ``evidence`` values reach the wire
    exactly as the handler wrote them — and a driver exception routinely carries
    a connection string.
    """

    def test_a_typed_errors_message_and_action_are_redacted(self):
        secret = "db://svc:hunter2@example-host:3306"
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=AuthError(
                        message=f"login refused for {secret}",
                        suggested_action=f"re-grant access at {secret}",
                    ).to_failure_details(),
                )
            ],
        )
        row = persist.build_check_result(_gate(workflow_slug=SLUG), verdict)
        assert row is not None
        assert "hunter2" not in row.model_dump_json()

    def test_evidence_values_are_redacted(self):
        # The model's own validator rejects secret-*named* keys; nothing checks
        # the values, so this is the gap redact() closes. Built as a wire
        # FailureDetails because evidence is derived from the producing error's
        # dataclass fields and cannot be passed to a leaf directly.
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    error=FailureDetails(
                        category=FailureCategory.AUTH,
                        code="AUTH",
                        retryable=False,
                        message="refused",
                        evidence={"dsn": "db://svc:hunter2@example-host:3306"},
                    ),
                )
            ],
        )
        row = persist.build_check_result(_gate(workflow_slug=SLUG), verdict)
        assert row is not None
        assert "hunter2" not in row.model_dump_json()

    def test_a_driver_message_is_redacted(self):
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="connectivity",
                    passed=False,
                    message="could not reach db://svc:hunter2@example-host:3306",
                )
            ],
        )
        row = persist.build_check_result(_gate(workflow_slug=SLUG), verdict)
        assert row is not None
        assert "hunter2" not in row.model_dump_json()

    def test_redaction_leaves_the_shape_alone(self):
        value = {"a": ["x", {"b": 1}], "c": True, "d": None}
        assert persist.redact_wire_value(value) == value


class TestTheWriteNeverFailsTheGate:
    def test_a_run_with_no_slug_schedules_nothing(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            persist, "post_check_result", lambda *a, **k: called.append(a)
        )
        persist.persist_check_result(
            _gate(), _verdict(), endpoint="http://store/x/check-results", timeout=1
        )
        assert called == []

    def test_no_running_loop_is_skipped_rather_than_raised(self):
        persist.persist_check_result(
            _gate(workflow_slug=SLUG),
            _verdict(),
            endpoint="http://store/x/check-results",
            timeout=1,
        )

    def test_it_returns_before_the_write_runs(self, monkeypatch):
        started: list[str] = []

        async def _slow(row, *, endpoint, timeout):
            started.append(row.workflow_slug)

        monkeypatch.setattr(persist, "post_check_result", _slow)

        async def scenario():
            persist.persist_check_result(
                _gate(workflow_slug=SLUG),
                _verdict(),
                endpoint="http://store/x/check-results",
                timeout=1,
            )
            assert started == []  # scheduled, not awaited
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert started == [SLUG]

        asyncio.run(scenario())

    def test_a_store_that_is_down_does_not_reach_the_caller(self, monkeypatch):
        async def _boom(row, *, endpoint, timeout):
            raise RuntimeError("store down")

        monkeypatch.setattr(persist, "post_check_result", _boom)

        async def scenario():
            persist.persist_check_result(
                _gate(workflow_slug=SLUG),
                _verdict(),
                endpoint="http://store/x/check-results",
                timeout=1,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(scenario())

    def test_an_unbuildable_row_does_not_reach_the_caller(self, monkeypatch):
        def _boom(*a, **k):
            raise ValueError("bad row")

        monkeypatch.setattr(persist, "build_check_result", _boom)
        persist.persist_check_result(
            _gate(workflow_slug=SLUG),
            _verdict(),
            endpoint="http://store/x/check-results",
            timeout=1,
        )


class TestTheWriteIsOffTheCallersPath:
    """The ordering the whole design rests on, without a socket.

    The real-transport version of this lives in
    ``tests/integration/test_preflight_persist.py``. This one is deterministic:
    it never opens a connection, so it holds under the unit suite's socket
    guard and under xdist.
    """

    def test_the_caller_returns_before_the_write_even_starts(self):
        # loop.create_task schedules without running, and persist_check_result
        # awaits nothing after it — so the write's first tick cannot happen
        # until the caller has already returned.
        trace: list[str] = []

        async def _slow(row, *, endpoint, timeout):
            trace.append("write:started")
            await asyncio.sleep(0.2)
            trace.append("write:finished")

        async def scenario():
            with mock.patch.object(persist, "post_check_result", _slow):
                task = persist.persist_check_result(
                    _gate(workflow_slug=SLUG),
                    _verdict(),
                    endpoint="http://store/x/check-results",
                    timeout=30,
                )
                trace.append("caller:returned")
                assert task is not None
                await task
            assert trace == ["caller:returned", "write:started", "write:finished"]

        asyncio.run(scenario())

    def test_a_slow_write_costs_the_caller_nothing(self):
        async def _slow(row, *, endpoint, timeout):
            await asyncio.sleep(0.2)

        async def scenario():
            with mock.patch.object(persist, "post_check_result", _slow):
                loop = asyncio.get_running_loop()
                started = loop.time()
                task = persist.persist_check_result(
                    _gate(workflow_slug=SLUG),
                    _verdict(),
                    endpoint="http://store/x/check-results",
                    timeout=30,
                )
                elapsed = loop.time() - started
                assert task is not None
                await task
            # Two orders of magnitude under the write it scheduled.
            assert elapsed < 0.02, f"scheduling cost {elapsed:.4f}s"

        asyncio.run(scenario())


class TestThePreflightResultsRouteContract:
    """Pins the address, route path and request shape the store expects.

    The write is fire-and-forget, so a mismatch drops rows without failing a
    run and nothing else here would catch it. See
    ``docs/standards/cross-repo-contracts.md`` before changing these.
    """

    def test_the_endpoint_is_the_whole_url_the_serving_app_publishes(self):
        """Host and path are frozen for every already-deployed SDK version."""
        assert constants.PREFLIGHT_RESULTS_ENDPOINT == (
            "http://system-workflows.system-workflows-app.svc.cluster.local:8000"
            "/continuous-preflight/check-results"
        )

    def test_the_row_carries_exactly_the_fields_the_route_accepts(self):
        """Field names are the wire contract; the receiver validates on them."""
        assert set(persist.PreflightCheckResult.model_fields) == {
            "workflow_slug",
            "origin",
            "payload",
            "extraction_method",
            "connection_qualified_name",
            "app_id",
            "app_version",
        }

    def test_the_closed_vocabularies_match_the_receivers(self):
        """A value the route's enums reject is a 422 and a dropped row."""
        assert {o.value for o in persist.PreflightResultOrigin} <= {
            "frontend",
            "continuous",
            "activity",
        }
        assert {m.value for m in persist.ExtractionMethod} == {"direct", "agent"}

    def test_no_authorization_header_is_sent(self):
        """The route is unauthenticated; putting auth on it drops every row."""
        sent: dict[str, object] = {}

        class _Response:
            is_success = True
            status_code = 201

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                sent.update(kwargs)
                return _Response()

        row = persist.PreflightCheckResult(
            workflow_slug=SLUG, payload={"preflight": {}}
        )
        with mock.patch.object(persist.httpx, "AsyncClient", lambda **_: _Client()):
            asyncio.run(
                persist.post_check_result(row, endpoint="http://store/x", timeout=1)
            )

        assert "Authorization" not in sent.get("headers", {})

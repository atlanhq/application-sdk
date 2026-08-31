"""The gate's store row: what it carries, and that writing it can never fail a run."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from application_sdk.contracts.base import Input
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
    """A minimal gate-eligible input, shaped like a connector's."""

    extraction_method: str = ""
    credential_guid: str = ""
    agent_json: object | None = None
    connection: dict = {}


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
        assert row.origin == persist.ORIGIN_ACTIVITY

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
        assert persist.extraction_method(gate) == persist.METHOD_AGENT

    @pytest.mark.parametrize("declared", ["", "direct", "s3", "offline"])
    def test_everything_else_is_direct(self, declared):
        gate = _gate(workflow_slug=SLUG, extraction_method=declared)
        assert persist.extraction_method(gate) == persist.METHOD_DIRECT

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
        assert persist.redact(value) == value


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


class TestTheWriteReallyLeavesTheProcess:
    """No stubs below the HTTP client: a real socket, over real httpx.

    Every other test here substitutes ``post_check_result``, which is the right
    seam for asserting *what* is sent — but it cannot show the send is genuinely
    off the caller's path, because a stub returns immediately whether the real
    thing would or not.

    Each scenario runs under a hard deadline, so a regression that makes the
    write blocking fails the test instead of hanging the suite.
    """

    DEADLINE = 10.0

    @staticmethod
    def _run(scenario) -> None:
        async def _bounded():
            await asyncio.wait_for(
                scenario(), timeout=TestTheWriteReallyLeavesTheProcess.DEADLINE
            )

        asyncio.run(_bounded())

    @staticmethod
    @asynccontextmanager
    async def _serving(handler):
        """A server on a free loopback port, torn down without waiting on handlers.

        Handlers are tracked and cancelled rather than awaited: one of them holds
        a connection open on purpose, and ``wait_closed`` would block on it.
        """
        live: set[asyncio.Task[None]] = set()

        async def _tracked(reader, writer):
            task = asyncio.current_task()
            assert task is not None
            live.add(task)
            try:
                await handler(reader, writer)
            finally:
                live.discard(task)
                writer.close()

        server = await asyncio.start_server(_tracked, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/continuous-preflight/check-results"
        finally:
            for task in list(live):
                task.cancel()
            server.close()

    @staticmethod
    def _respond(status: bytes, body: bytes = b""):
        async def _handler(reader, writer):
            await reader.read(1)
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()

        return _handler

    def _persist(self, endpoint: str, timeout: float = 5.0):
        return persist.persist_check_result(
            _gate(workflow_slug=SLUG), _verdict(), endpoint=endpoint, timeout=timeout
        )

    def test_the_caller_returns_before_the_store_answers(self):
        # The point of the whole design: the app endpoint opens a Polaris catalog,
        # may create the table and commits Parquet. Were this ever awaited, every
        # gated run in the fleet would carry that latency.
        connected = asyncio.Event()

        async def _never_answer(reader, writer):
            connected.set()
            await asyncio.Event().wait()  # held open until cancelled

        async def scenario():
            async with self._serving(_never_answer) as endpoint:
                loop = asyncio.get_running_loop()
                started = loop.time()
                task = self._persist(endpoint, timeout=30)
                assert loop.time() - started < 0.05
                assert task is not None and not task.done()

                # The request does reach the socket, and is still in flight long
                # after the caller moved on.
                await connected.wait()
                assert not task.done()

                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._run(scenario)

    def test_a_store_that_refuses_the_connection_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"200 OK")) as endpoint:
                pass  # the port is free again, and nothing is listening on it
            task = self._persist(endpoint, timeout=2)
            assert task is not None
            # Awaiting re-raises what the task caught, which no production caller
            # does — the guarantee under test is that scheduling it did not raise.
            await asyncio.gather(task, return_exceptions=True)
            assert isinstance(task.exception(), Exception)

        self._run(scenario)

    def test_a_rejected_row_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"422 Unprocessable Entity")) as e:
                task = self._persist(e)
                assert task is not None
                await task
                assert task.exception() is None

        self._run(scenario)

    def test_a_broken_store_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"503 Service Unavailable")) as e:
                task = self._persist(e)
                assert task is not None
                await task
                assert task.exception() is None

        self._run(scenario)

    def test_a_rejection_never_echoes_the_verdict_into_a_log(self, caplog):
        # A 4xx body is FastAPI's validation error, which renders the input it
        # rejected — and the input is the verdict.
        body = b'{"detail":"invalid payload: db://svc:hunter2@example-host:3306"}'

        async def scenario():
            async with self._serving(
                self._respond(b"422 Unprocessable Entity", body)
            ) as endpoint:
                task = self._persist(endpoint)
                assert task is not None
                await task

        with caplog.at_level("DEBUG"):
            self._run(scenario)
        assert "hunter2" not in caplog.text
        assert "422" in caplog.text  # the status is what does get reported

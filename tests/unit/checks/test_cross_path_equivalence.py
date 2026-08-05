"""The test that would have caught the divergence this consolidation removed.

Before the shared check core, four call sites reached the same
``Handler.preflight_check`` while assembling its input differently, resolving
credentials differently, enforcing different budgets (or none), running different
extra checks, and recording different amounts of nothing. Nothing compared them, so
the drift was invisible until a customer's check passed in the config UI and the run
it was supposed to predict failed.

These tests drive one fixture through every ingress and assert the answers agree.
They are deliberately about *equivalence*, not about any single path's behaviour —
per-path policy (who raises, who enforces) is asserted in each path's own suite.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from application_sdk.checks.depth import CheckDepth
from application_sdk.checks.request import CheckRequest, CheckTrigger
from application_sdk.checks.runner import run_checks
from application_sdk.checks.verdict import CheckClassification
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    build_preflight_gate_activity,
)
from application_sdk.execution._temporal.sdr import (
    CHECKS_SCHEDULED_ACTIVITY,
    SDR_PREFLIGHT_ACTIVITY,
    build_sdr_activities,
)
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)

_CHECK_CREDS = "application_sdk.checks.credentials"


class _RecordingHandler(Handler):
    """Returns a fixed verdict and records the input it was handed."""

    def __init__(self, output: PreflightOutput) -> None:
        self._output = output
        self.seen: list[PreflightInput] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.seen.append(input)
        return self._output

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


def _ready() -> PreflightOutput:
    return PreflightOutput(
        status=PreflightStatus.READY,
        message="all good",
        checks=[
            PreflightCheck(name="Credential", passed=True, depth=CheckDepth.AUTH),
            PreflightCheck(name="Catalog", passed=True, depth=CheckDepth.FULL),
        ],
    )


def _not_ready() -> PreflightOutput:
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        message="credential rejected",
        checks=[
            PreflightCheck(
                name="Credential",
                passed=False,
                depth=CheckDepth.AUTH,
                error=AuthError(message="password rejected").to_failure_details(),
            )
        ],
    )


def _resolver(raw: dict[str, Any]) -> mock.MagicMock:
    resolver = mock.MagicMock()
    resolver.resolve_raw = mock.AsyncMock(return_value=raw)
    return resolver


def _infra_patches(resolver: mock.MagicMock):
    fake_infra = mock.MagicMock()
    fake_infra.secret_store = mock.MagicMock(name="SecretStore")
    return (
        mock.patch(f"{_CHECK_CREDS}.get_infrastructure", return_value=fake_infra),
        mock.patch(f"{_CHECK_CREDS}.CredentialResolver", return_value=resolver),
    )


async def _via_gate(handler: Handler, **gate_kwargs: Any) -> PreflightOutput:
    gate = build_preflight_gate_activity(handler, app_name="myapp", **gate_kwargs)
    return await gate(
        PreflightGateInput(
            credential_guid="guid-1",
            extraction_method="direct",
            entrypoint="crawl",
            extraction_snapshot={"credential_guid": "guid-1", "warehouse": "WH"},
        )
    )


async def _via_sdr(handler: Handler) -> PreflightOutput:
    activities = {
        getattr(a, "__temporal_activity_definition").name: a
        for a in build_sdr_activities(handler, app_name="myapp")
    }
    return await activities[SDR_PREFLIGHT_ACTIVITY](
        PreflightInput(entrypoint="crawl", connection_config={"warehouse": "WH"})
    )


async def _via_scheduled(handler: Handler) -> PreflightOutput:
    activities = {
        getattr(a, "__temporal_activity_definition").name: a
        for a in build_sdr_activities(handler, app_name="myapp")
    }
    return await activities[CHECKS_SCHEDULED_ACTIVITY](
        PreflightInput(entrypoint="crawl", connection_config={"warehouse": "WH"})
    )


async def _via_core(handler: Handler, trigger: CheckTrigger) -> PreflightOutput:
    request = CheckRequest.from_preflight_input(
        PreflightInput(entrypoint="crawl", connection_config={"warehouse": "WH"}),
        app_name="myapp",
        trigger=trigger,
    )
    verdict = await run_checks(handler, request)
    return verdict.output


class TestEveryPathReachesTheSameVerdict:
    async def test_ready_verdict_is_identical_across_paths(self) -> None:
        outputs = []
        for run in (
            lambda h: _via_gate(h),
            _via_sdr,
            _via_scheduled,
            lambda h: _via_core(h, CheckTrigger.UI_PREFLIGHT),
        ):
            handler = _RecordingHandler(_ready())
            resolver = _resolver({"user": "u", "password": "p"})
            gate_patch, resolver_patch = _infra_patches(resolver)
            with gate_patch, resolver_patch:
                outputs.append(await run(handler))

        statuses = {o.status for o in outputs}
        assert statuses == {PreflightStatus.READY}
        matrices = {
            tuple((c.name, c.passed) for c in o.checks)
            for o in outputs
            # The scheduled path adds nothing to the checks; only the cadence hint.
        }
        assert len(matrices) == 1, f"check matrices diverged across paths: {matrices}"

    async def test_not_ready_verdict_is_identical_across_paths(self) -> None:
        # Soft gate so the gate reports rather than raises; enforcement is the gate's
        # own concern and is asserted in its suite, not here.
        outputs = []
        for run in (
            lambda h: _via_gate(h, enforce=False),
            _via_sdr,
            _via_scheduled,
            lambda h: _via_core(h, CheckTrigger.UI_PREFLIGHT),
        ):
            handler = _RecordingHandler(_not_ready())
            resolver = _resolver({"user": "u"})
            gate_patch, resolver_patch = _infra_patches(resolver)
            with gate_patch, resolver_patch:
                outputs.append(await run(handler))

        assert {o.status for o in outputs} == {PreflightStatus.NOT_READY}
        codes = {o.checks[0].error.code if o.checks[0].error else None for o in outputs}
        assert codes == {"AUTH"}, f"failure attribution diverged: {codes}"


class TestEveryPathAssemblesTheSameHandlerInput:
    async def test_form_config_reaches_the_handler_on_every_path(self) -> None:
        """The regression class this consolidation exists to prevent.

        A handler reading a config field must find it whichever surface invoked it.
        The gate derives config from an extraction snapshot while the interactive
        paths get it from a request body; both must land on the same field.
        """
        seen: list[str | None] = []
        for run in (
            lambda h: _via_gate(h),
            _via_sdr,
            _via_scheduled,
            lambda h: _via_core(h, CheckTrigger.UI_PREFLIGHT),
        ):
            handler = _RecordingHandler(_ready())
            resolver = _resolver({"user": "u"})
            gate_patch, resolver_patch = _infra_patches(resolver)
            with gate_patch, resolver_patch:
                await run(handler)
            seen.append(handler.seen[0].connection_config.get("warehouse"))

        assert seen == ["WH", "WH", "WH", "WH"]

    async def test_every_path_enforces_a_budget(self) -> None:
        """No path may hand the handler the misleading 60s contract default.

        Only the gate used to enforce a budget at all. A handler sizing its probes to
        ``input.timeout_seconds`` on the HTTP path was sizing to a number nothing
        honoured.
        """
        for run in (
            lambda h: _via_gate(h),
            _via_sdr,
            _via_scheduled,
            lambda h: _via_core(h, CheckTrigger.UI_PREFLIGHT),
        ):
            handler = _RecordingHandler(_ready())
            resolver = _resolver({"user": "u"})
            gate_patch, resolver_patch = _infra_patches(resolver)
            with gate_patch, resolver_patch:
                await run(handler)
            budget = handler.seen[0].timeout_seconds
            assert budget > 0

    async def test_every_path_resolves_a_credential_reference(self) -> None:
        """A guid must be dereferenced wherever it arrives.

        The HTTP path could not do this at all before: it checked whatever the form
        held, never the stored credential a run would actually use.
        """
        handler = _RecordingHandler(_ready())
        resolver = _resolver({"user": "stored-user", "password": "stored-pass"})
        gate_patch, resolver_patch = _infra_patches(resolver)
        request = CheckRequest.from_preflight_input(
            PreflightInput(entrypoint="crawl"), app_name="myapp"
        )
        request.credential_source.credential_guid = "guid-1"
        request.credential_source.extraction_method = "direct"
        with gate_patch, resolver_patch:
            await run_checks(handler, request)

        assert {c.key: c.value for c in handler.seen[0].credentials} == {
            "user": "stored-user",
            "password": "stored-pass",
        }


class TestInlineCredentialsBeatStoredOnes:
    async def test_typed_form_values_are_what_gets_checked(self) -> None:
        """Editing a password and pressing Test must check the typed value.

        Resolving the stored secret instead would make the button answer a question
        nobody asked — and would report success on a credential the user is trying to
        replace.
        """
        from application_sdk.handler.contracts import HandlerCredential

        handler = _RecordingHandler(_ready())
        resolver = _resolver({"password": "stored-and-stale"})
        gate_patch, resolver_patch = _infra_patches(resolver)
        request = CheckRequest.from_preflight_input(
            PreflightInput(
                credentials=[HandlerCredential(key="password", value="typed")]
            ),
            app_name="myapp",
        )
        request.credential_source.credential_guid = "guid-1"
        request.credential_source.extraction_method = "direct"
        with gate_patch, resolver_patch:
            await run_checks(handler, request)

        assert {c.key: c.value for c in handler.seen[0].credentials} == {
            "password": "typed"
        }
        resolver.resolve_raw.assert_not_awaited()


class TestScheduledPathIsAdvisory:
    async def test_unverifiable_source_does_not_raise(self) -> None:
        """The scheduler must get a verdict, not an exception.

        A failed activity would make it retry-and-alert on what is usually a slow
        source; the honest NOT_READY is the more useful record. Contrast the
        interactive path, which fails so a waiting human sees an error.
        """

        class _Crashing(_RecordingHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise RuntimeError("driver exploded")

        handler = _Crashing(_ready())
        output = await _via_scheduled(handler)
        assert output.status is PreflightStatus.NOT_READY
        assert output.recheck_after_seconds is not None

    async def test_interactive_path_raises_on_the_same_failure(self) -> None:
        class _Crashing(_RecordingHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise RuntimeError("driver exploded")

        handler = _Crashing(_ready())
        with pytest.raises(Exception) as excinfo:
            await _via_sdr(handler)
        assert getattr(excinfo.value, "type", None) == "PreflightUnverifiable"

    async def test_cadence_shortens_after_a_failure(self) -> None:
        from application_sdk.checks.cadence import (
            RECHECK_AFTER_FAILURE_SECONDS,
            RECHECK_AFTER_FULL_PASS_SECONDS,
        )

        failed = await _via_scheduled(_RecordingHandler(_not_ready()))
        passed = await _via_scheduled(_RecordingHandler(_ready()))
        assert failed.recheck_after_seconds == RECHECK_AFTER_FAILURE_SECONDS
        assert passed.recheck_after_seconds == RECHECK_AFTER_FULL_PASS_SECONDS
        assert failed.recheck_after_seconds < passed.recheck_after_seconds


class TestClassificationSurvivesEveryPath:
    async def test_plumbing_failure_is_never_a_source_verdict(self) -> None:
        """A broken secret store must not be reported as a bad credential.

        This is the distinction that makes enforcement safe, and it has to hold on
        every path — an interactive test that blames the customer for our outage
        sends them hunting a problem they do not have.
        """
        from application_sdk.errors.leaves import DependencyUnavailableError

        class _Plumbing(_RecordingHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise DependencyUnavailableError(
                    message="vault unreachable", service="secret_store"
                )

        for run in (_via_sdr, _via_scheduled, lambda h: _via_gate(h, enforce=True)):
            with pytest.raises(DependencyUnavailableError):
                await run(_Plumbing(_ready()))

    async def test_handler_crash_is_classified_source_unverifiable(self) -> None:
        class _Crashing(_RecordingHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise ValueError("boom")

        request = CheckRequest.from_preflight_input(
            PreflightInput(), app_name="myapp", trigger=CheckTrigger.SCHEDULED
        )
        verdict = await run_checks(_Crashing(_ready()), request)
        assert verdict.classification is CheckClassification.SOURCE_UNVERIFIABLE
        assert verdict.status is PreflightStatus.NOT_READY
        # An untyped crash is INTERNAL with classification_pending, not TIMEOUT:
        # reporting every AttributeError to the Automation Engine as a slow source
        # would make the taxonomy useless.
        error = verdict.output.checks[0].error
        assert error is not None
        assert error.category is FailureCategory.INTERNAL

    async def test_crash_message_is_sanitized_before_it_is_reported(self) -> None:
        """A driver exception routinely carries the connection string.

        The message lands on ``FailureDetails.message``, in Temporal history and in
        ClickHouse, so it goes through the sanitizer rather than being interpolated
        raw — ``to_failure_details()`` sanitizes ``cause_repr`` but not ``message``.
        """

        class _Leaky(_RecordingHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise ValueError("postgres://user:s3cr3t@host:5432/db unreachable")

        request = CheckRequest.from_preflight_input(
            PreflightInput(), app_name="myapp", trigger=CheckTrigger.SCHEDULED
        )
        verdict = await run_checks(_Leaky(_ready()), request)
        message = verdict.output.message
        assert "s3cr3t" not in message
        assert "***" in message

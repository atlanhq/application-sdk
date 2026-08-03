"""Pins the two output shapes that other systems parse.

Both have already regressed once by being tidied up, and neither failure was
visible from inside this repo:

* The **SageV2 widget payload** renders ``success ? successMessage : failureMessage``
  with no fallback, so dropping either field leaves a blank detail panel on a failed
  check (DBBI-665, WARE-1250).
* The **outcome event** is what connector-pulse pattern-matches in ClickHouse. An
  attribute that stops being emitted, or a ``check_matrix`` row that grows a field,
  breaks a dashboard nothing here compiles against.

These are golden tests on purpose: they compare whole structures rather than spot-
checking fields, because the failure mode is an *omission*, and an assertion that
names only the fields it expects cannot see one.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from application_sdk.checks.outcome import (
    EMPTY_CHECK_MATRIX,
    GATE_MODE_NOT_APPLICABLE,
    PREFLIGHT_OUTCOME_EVENT,
    check_matrix_json,
)
from application_sdk.checks.outcome import emit as emit_outcome
from application_sdk.checks.projections import (
    envelope_success,
    to_runtime_summary,
    to_v2_widget_payload,
)
from application_sdk.checks.request import CheckTrigger
from application_sdk.checks.verdict import CheckClassification, CheckVerdict
from application_sdk.errors.leaves import AuthError
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)


def _mixed_output() -> PreflightOutput:
    return PreflightOutput(
        status=PreflightStatus.PARTIAL,
        message="one advisory check failed",
        total_duration_ms=1234.5,
        checks=[
            PreflightCheck(
                name="AuthCheck", passed=True, message="signed in", duration_ms=12.0
            ),
            PreflightCheck(
                name="TablesCheck",
                passed=False,
                message="ignored when error is set",
                duration_ms=34.0,
                error=AuthError(
                    message="no SELECT on schema", suggested_action="grant SELECT"
                ).to_failure_details(),
            ),
        ],
    )


class TestSageWidgetPayload:
    def test_payload_shape_is_frozen(self) -> None:
        assert to_v2_widget_payload(_mixed_output().checks) == {
            "authCheck": {
                "success": True,
                "message": "signed in",
                "successMessage": "signed in",
                "failureMessage": "",
            },
            "tablesCheck": {
                "success": False,
                # A failed check's typed error wins over its message field.
                "message": "no SELECT on schema",
                "successMessage": "",
                "failureMessage": "no SELECT on schema",
            },
        }

    def test_envelope_success_reports_execution_not_pass(self) -> None:
        # The widget short-circuits on !success and skips the per-check render loop,
        # so this must stay true for a PARTIAL/NOT_READY result that produced rows —
        # otherwise the user sees "Check failed" over a blank panel (DBBI-665).
        assert envelope_success(_mixed_output().checks) is True
        assert envelope_success([]) is False

    def test_runtime_summary_carries_the_canonical_status(self) -> None:
        summary = to_runtime_summary(_mixed_output())
        assert summary["status"] == "partial"
        assert summary["message"] == "one advisory check failed"
        assert summary["total_duration_ms"] == 1234.5
        # suggested_action is surfaced only for the failed check, following the same
        # precedence rule as the message.
        assert summary["checks"][0].get("suggested_action") is None
        assert summary["checks"][1]["suggested_action"] == "grant SELECT"


class TestCheckMatrix:
    def test_row_fields_are_frozen(self) -> None:
        rows = json.loads(check_matrix_json(_mixed_output().checks))
        assert [set(r) for r in rows] == [
            {"name", "passed", "error_code", "duration_ms"},
            {"name", "passed", "error_code", "duration_ms"},
        ]
        assert rows[1]["error_code"] == "AUTH"

    def test_no_user_facing_text_leaks_into_the_matrix(self) -> None:
        # Messages and evidence stay in the activity result, which is where a human
        # looks; the matrix must stay small and free of source-adjacent text.
        serialized = check_matrix_json(_mixed_output().checks)
        assert "no SELECT on schema" not in serialized
        assert "signed in" not in serialized

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_durations_become_zero(self, bad: float) -> None:
        # orjson emits null for these; normalizing keeps the ClickHouse column
        # numeric for downstream JSONExtract, and must never raise — a raise here
        # would lose the whole event.
        rows = json.loads(
            check_matrix_json([PreflightCheck(name="x", passed=True, duration_ms=bad)])
        )
        assert rows[0]["duration_ms"] == 0.0

    def test_empty_matrix_sentinel_is_valid_json(self) -> None:
        # Emitted rather than omitted so a consumer can parse unconditionally.
        assert json.loads(EMPTY_CHECK_MATRIX) == []


class TestOutcomeEventAttributes:
    def _emit(self, **verdict_kwargs) -> dict:
        fields: dict = {
            "output": _mixed_output(),
            "classification": CheckClassification.VERDICT,
            "app_name": "myapp",
            "entrypoint": "crawl",
            "duration_ms": 99.94,
            "budget_seconds": 150,
            "attempt": 2,
        }
        fields.update(verdict_kwargs)
        verdict = CheckVerdict(**fields)
        with mock.patch("application_sdk.checks.outcome.logger") as m:
            emit_outcome(verdict, outcome="proceeded", reason="partial")
        (call,) = [
            c for c in m.info.call_args_list if c.args[0] == PREFLIGHT_OUTCOME_EVENT
        ]
        return call.kwargs

    def test_attribute_set_is_frozen(self) -> None:
        # Literal names, not the constants: these strings are what connector-pulse
        # queries in ClickHouse, so renaming a constant must fail here rather than
        # sail through a test that reads the new name from the same source.
        assert set(self._emit()) == {
            "outcome",
            "reason",
            "app_name",
            "entrypoint",
            "checks",
            "trigger",
            "check_matrix",
            "gate_mode",
            "gate_classification",
            "gate_duration_ms",
            "gate_timeout_seconds",
            "gate_attempt",
        }

    def test_values_are_carried_through(self) -> None:
        kwargs = self._emit()
        assert kwargs["outcome"] == "proceeded"
        assert kwargs["reason"] == "partial"
        assert kwargs["app_name"] == "myapp"
        assert kwargs["entrypoint"] == "crawl"
        assert kwargs["checks"] == 2
        assert kwargs["gate_classification"] == "verdict"
        assert kwargs["gate_duration_ms"] == 99.9
        assert kwargs["gate_timeout_seconds"] == 150
        assert kwargs["gate_attempt"] == 2

    def test_missing_entrypoint_reports_implicit(self) -> None:
        assert self._emit(entrypoint="")["entrypoint"] == "<implicit>"

    def test_non_enforcing_triggers_do_not_borrow_a_posture(self) -> None:
        """``gate_mode`` must not claim soft/hard on a run nothing can block.

        Only the pre-run gate has a posture. A distinct value keeps an existing
        ``gate_mode='hard'`` query exactly as selective as it was, so newly-emitted
        UI and scheduled rows fall outside it rather than being mis-binned into it.
        """
        for trigger in (
            CheckTrigger.UI_AUTH,
            CheckTrigger.UI_PREFLIGHT,
            CheckTrigger.SDR,
            CheckTrigger.SCHEDULED,
        ):
            kwargs = self._emit(trigger=trigger)
            assert kwargs["gate_mode"] == GATE_MODE_NOT_APPLICABLE
            assert kwargs["trigger"] == trigger.value

    def test_trigger_is_present_on_every_row(self) -> None:
        """The attribute that makes the widened event stream separable.

        Consumers that only want gate rows filter on this; without it on every row a
        UI test would be indistinguishable from a real pre-run verdict.
        """
        assert self._emit(trigger=CheckTrigger.PRE_RUN)["trigger"] == "pre_run"

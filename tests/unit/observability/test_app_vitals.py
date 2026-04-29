"""Unit tests for application_sdk.observability.app_vitals.

These tests focus on pure helper functions and on the interceptor's
public contracts, with all side effects (logger, Temporal info,
correlation ContextVar, trace context, resource sampler) mocked.

NO REAL THREADS, NO REAL ASYNCIO LOOPS, NO REAL NETWORK / OTEL.
Each test must complete in < 0.5s.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from application_sdk.observability import app_vitals as av

# ───────────────────────────────────────────────────────────────────────────
# _extract_assets_processed
# ───────────────────────────────────────────────────────────────────────────


class TestExtractAssetsProcessed:
    def test_none_returns_none(self):
        assert av._extract_assets_processed(None) is None

    def test_object_with_assets_processed_attr(self):
        obj = SimpleNamespace(assets_processed=42)
        assert av._extract_assets_processed(obj) == 42

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"total_record_count": 7}, 7),
            ({"record_count": 5}, 5),
            ({"records_processed": 9}, 9),
        ],
    )
    def test_dict_aliases(self, payload: dict[str, int], expected: int):
        assert av._extract_assets_processed(payload) == expected

    def test_first_recognized_attribute_wins(self):
        # `assets_processed` is checked before `total_record_count`.
        obj = SimpleNamespace(assets_processed=1, total_record_count=999)
        assert av._extract_assets_processed(obj) == 1

    def test_negative_values_ignored(self):
        # Negative is rejected; falls through and returns None.
        assert av._extract_assets_processed({"assets_processed": -1}) is None

    def test_zero_is_valid(self):
        assert av._extract_assets_processed({"assets_processed": 0}) == 0

    def test_non_int_value_ignored(self):
        assert av._extract_assets_processed({"assets_processed": "abc"}) is None

    def test_unknown_shape_returns_none(self):
        # Plain string with no known attributes / keys.
        assert av._extract_assets_processed("just-a-string") is None

    def test_attribute_returning_none_is_skipped(self):
        obj = SimpleNamespace(assets_processed=None, record_count=12)
        assert av._extract_assets_processed(obj) == 12


# ───────────────────────────────────────────────────────────────────────────
# _format_stack_trace
# ───────────────────────────────────────────────────────────────────────────


class TestFormatStackTrace:
    def test_normal_exception_returns_traceback(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            out = av._format_stack_trace(e)
        assert "ValueError" in out
        assert "boom" in out

    def test_truncated_to_2000_chars(self):
        try:
            raise ValueError("x" * 5000)
        except ValueError as e:
            out = av._format_stack_trace(e)
        assert len(out) <= 2000

    def test_format_failure_falls_back_to_empty_string(self):
        """Force the outer try to fail so the fallback logger path runs."""
        bad = mock.MagicMock(spec=BaseException)
        # __traceback__ access will raise
        type(bad).__traceback__ = mock.PropertyMock(side_effect=RuntimeError("t"))
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger"
        ) as mock_get_logger:
            mock_get_logger.return_value = mock.MagicMock()
            out = av._format_stack_trace(bad)
        assert out == ""


# ───────────────────────────────────────────────────────────────────────────
# _compute_error_fingerprint
# ───────────────────────────────────────────────────────────────────────────


class TestComputeErrorFingerprint:
    def test_deterministic(self):
        f1 = av._compute_error_fingerprint("act", "timeout", "TimeoutError")
        f2 = av._compute_error_fingerprint("act", "timeout", "TimeoutError")
        assert f1 == f2

    def test_different_inputs_different_fingerprints(self):
        f1 = av._compute_error_fingerprint("act", "timeout", "TimeoutError")
        f2 = av._compute_error_fingerprint("act", "timeout", "ConnectionError")
        assert f1 != f2

    def test_length_is_16(self):
        assert len(av._compute_error_fingerprint("a", "b", "c")) == 16


# ───────────────────────────────────────────────────────────────────────────
# _build_common_attrs / _build_workflow_identity_attrs / _get_correlation_id
# ───────────────────────────────────────────────────────────────────────────


class TestGetCorrelationId:
    @pytest.mark.parametrize(
        ("return_value", "side_effect", "expected"),
        [
            (mock.Mock(correlation_id="cid-1"), None, "cid-1"),
            (None, None, ""),
            (mock.Mock(correlation_id=""), None, ""),
            (None, RuntimeError("boom"), ""),
        ],
    )
    def test_get_correlation_id(
        self,
        return_value: Any,
        side_effect: Exception | None,
        expected: str,
    ) -> None:
        with mock.patch(
            "application_sdk.observability.correlation.get_correlation_context",
            return_value=return_value,
            side_effect=side_effect,
        ):
            assert av._get_correlation_id() == expected


class TestBuildCommonAttrs:
    def test_includes_app_vitals_marker_and_app_name(self):
        with mock.patch(
            "application_sdk.observability.app_vitals.get_trace_context",
            return_value=("tid", "sid"),
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals._get_correlation_id",
                return_value="c-1",
            ):
                attrs = av._build_common_attrs()
        assert attrs["app_vitals"] == "true"
        assert attrs["trace_id"] == "tid"
        assert attrs["span_id"] == "sid"
        assert attrs["correlation_id"] == "c-1"
        assert "app_name" in attrs


class TestBuildWorkflowIdentityAttrs:
    def test_with_parent(self):
        info = SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=SimpleNamespace(workflow_id="parent-wf", run_id="parent-run"),
            continued_run_id="cont-1",
        )
        out = av._build_workflow_identity_attrs(info)
        assert out["workflow_id"] == "wf-1"
        assert out["parent_workflow_id"] == "parent-wf"
        assert out["parent_run_id"] == "parent-run"
        assert out["continued_run_id"] == "cont-1"

    def test_without_parent_returns_empty_strings(self):
        info = SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=None,
            continued_run_id=None,
        )
        out = av._build_workflow_identity_attrs(info)
        assert out["parent_workflow_id"] == ""
        assert out["parent_run_id"] == ""
        assert out["continued_run_id"] == ""

    def test_falsy_string_fields_default_to_empty(self):
        info = SimpleNamespace(
            workflow_id=None,
            run_id=None,
            workflow_type=None,
            task_queue=None,
            namespace=None,
            parent=None,
            continued_run_id=None,
        )
        out = av._build_workflow_identity_attrs(info)
        assert out["workflow_id"] == ""
        assert out["workflow_type"] == ""


# ───────────────────────────────────────────────────────────────────────────
# _detect_preflight_passed / _detect_circuit_breaker
# ───────────────────────────────────────────────────────────────────────────


class TestDetectPreflightPassed:
    def test_no_preflight_acts_returns_none(self):
        acts = [{"activity_type": "fetch_databases", "status": "succeeded"}]
        assert av._detect_preflight_passed(acts) is None

    def test_all_preflight_succeeded_returns_true(self):
        acts = [
            {"activity_type": "preflight_check", "status": "succeeded"},
            {"activity_type": "setup_session", "status": "succeeded"},
            {"activity_type": "connection_check", "status": "succeeded"},
        ]
        assert av._detect_preflight_passed(acts) is True

    def test_any_preflight_failed_returns_false(self):
        acts = [
            {"activity_type": "preflight_check", "status": "succeeded"},
            {"activity_type": "setup_session", "status": "failed"},
        ]
        assert av._detect_preflight_passed(acts) is False

    def test_preflight_pending_returns_none(self):
        acts = [{"activity_type": "preflight_check", "status": "pending"}]
        assert av._detect_preflight_passed(acts) is None


class TestDetectCircuitBreaker:
    def test_match_in_error_class(self):
        failed = [{"error_class": "CircuitBreakerError"}]
        assert av._detect_circuit_breaker(failed) is True

    def test_match_in_error_message(self):
        failed = [{"error_message": "circuit breaker open for foo"}]
        assert av._detect_circuit_breaker(failed) is True

    def test_match_in_cause_chain(self):
        failed = [{"error_cause_chain": ["ApplicationError: CircuitBreakerTriggered"]}]
        assert av._detect_circuit_breaker(failed) is True

    def test_no_match_returns_false(self):
        failed = [
            {
                "error_class": "RuntimeError",
                "error_message": "boom",
                "error_cause_chain": ["RuntimeError: boom"],
            }
        ]
        assert av._detect_circuit_breaker(failed) is False

    def test_empty_failed_list_returns_false(self):
        assert av._detect_circuit_breaker([]) is False


# ───────────────────────────────────────────────────────────────────────────
# _emit_log_event
# ───────────────────────────────────────────────────────────────────────────


class TestEmitLogEvent:
    def test_writes_compact_summary_to_stdout(self, capsys):
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger"
        ) as gl:
            gl.return_value = mock.MagicMock()
            av._emit_log_event(
                "app_vitals.test",
                {
                    "app_name": "x",
                    "status": "succeeded",
                    "workflow_id": "wf-1",
                    "duration_ms": 12.3,
                    "ignored": "private",
                },
            )
        out = capsys.readouterr().out
        assert out.startswith("APP_VITALS | app_vitals.test |")
        # The JSON body must be valid and only contain whitelisted keys.
        json_body = out.split("|", 2)[2].strip()
        decoded = json.loads(json_body)
        assert "ignored" not in decoded
        assert decoded["status"] == "succeeded"

    def test_invokes_info_when_status_succeeded(self):
        mock_logger = mock.MagicMock()
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=mock_logger,
        ):
            av._emit_log_event("ev", {"status": "succeeded"})
        mock_logger.info.assert_called_once()

    def test_invokes_error_when_status_failed(self):
        mock_logger = mock.MagicMock()
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            return_value=mock_logger,
        ):
            av._emit_log_event("ev", {"status": "failed"})
        mock_logger.error.assert_called_once()

    def test_logger_failure_is_swallowed(self):
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger",
            side_effect=RuntimeError("loader-broke"),
        ):
            # Must not raise.
            av._emit_log_event("ev", {"status": "succeeded"})

    def test_omits_empty_values_from_console_summary(self, capsys):
        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger"
        ) as gl:
            gl.return_value = mock.MagicMock()
            av._emit_log_event(
                "ev",
                {
                    "app_name": "x",
                    "workflow_id": "",  # empty -> excluded
                    "status": "succeeded",
                    "duration_ms": None,  # None -> excluded
                },
            )
        out = capsys.readouterr().out
        body = out.split("|", 2)[2].strip()
        decoded = json.loads(body)
        assert "workflow_id" not in decoded
        assert "duration_ms" not in decoded


# ───────────────────────────────────────────────────────────────────────────
# _track_activity_completion (succeeded vs failed)
# ───────────────────────────────────────────────────────────────────────────


class TestTrackActivityCompletion:
    @pytest.mark.asyncio
    async def test_succeeded_path_sets_status_and_duration(self):
        record: dict[str, Any] = {
            "activity_type": "x",
            "start_ns": 1_000_000_000,  # 1s in ns
            "status": "pending",
        }

        async def coro():
            return "ok"

        with mock.patch(
            "application_sdk.observability.app_vitals.time.monotonic_ns",
            return_value=2_000_000_000,
        ):
            result = await av._track_activity_completion(record, coro())
        assert result == "ok"
        assert record["status"] == "succeeded"
        assert record["duration_ms"] == 1000.0

    @pytest.mark.asyncio
    async def test_failed_path_records_error_and_reraises(self):
        record: dict[str, Any] = {
            "activity_type": "x",
            "start_ns": 1_000_000_000,
            "status": "pending",
        }

        async def coro():
            raise ValueError("boom")

        with mock.patch(
            "application_sdk.observability.app_vitals.time.monotonic_ns",
            return_value=2_000_000_000,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.classify_error",
                return_value="internal",
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals.extract_cause_chain",
                    return_value=[],
                ):
                    with pytest.raises(ValueError):
                        await av._track_activity_completion(record, coro())
        assert record["status"] == "failed"
        assert record["error_class"] == "ValueError"
        assert record["error_message"] == "boom"
        assert record["error_type"] == "internal"
        assert record["duration_ms"] == 1000.0


# ───────────────────────────────────────────────────────────────────────────
# AppVitalsInterceptor — exposed factory contract
# ───────────────────────────────────────────────────────────────────────────


class TestAppVitalsInterceptor:
    def test_workflow_interceptor_class_returns_inbound_class(self):
        intc = av.AppVitalsInterceptor()
        assert (
            intc.workflow_interceptor_class(mock.MagicMock())
            is av._AppVitalsWorkflowInboundInterceptor
        )

    def test_intercept_activity_wraps_with_inbound(self):
        intc = av.AppVitalsInterceptor()
        next_ = mock.MagicMock()
        wrapped = intc.intercept_activity(next_)
        assert isinstance(wrapped, av._AppVitalsActivityInboundInterceptor)


# ───────────────────────────────────────────────────────────────────────────
# _AppVitalsWorkflowInboundInterceptor._build_summary_attrs
# ───────────────────────────────────────────────────────────────────────────


class TestBuildSummaryAttrs:
    def _make_inbound(self):
        # Construct without going through __init__ so we don't need a real
        # next_ inbound interceptor.
        inbound = av._AppVitalsWorkflowInboundInterceptor.__new__(
            av._AppVitalsWorkflowInboundInterceptor
        )
        inbound._activity_records = []
        inbound._child_workflow_records = []
        return inbound

    def _make_info(self):
        return SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=None,
            continued_run_id=None,
        )

    def test_summary_with_failed_first_failure_and_bottleneck(self):
        inbound = self._make_inbound()
        inbound._activity_records = [
            {
                "activity_type": "preflight_check",
                "status": "succeeded",
                "duration_ms": 5.0,
                "start_ns": 1,
            },
            {
                "activity_type": "fetch",
                "status": "failed",
                "duration_ms": 10.0,
                "start_ns": 2,
                "error_type": "internal",
            },
            {
                "activity_type": "transform",
                "status": "succeeded",
                "duration_ms": 100.0,
                "start_ns": 3,
            },
        ]
        attrs = inbound._build_summary_attrs(
            common={"app_vitals": "true"},
            info=self._make_info(),
            wf_status="failed",
            wf_duration_ms=200.0,
        )
        assert attrs["total_activities"] == 3
        assert attrs["succeeded_activities"] == 2
        assert attrs["failed_activities"] == 1
        assert attrs["sum_activity_duration_ms"] == 115.0
        assert attrs["preflight_passed"] is True
        assert attrs["circuit_breaker_tripped"] is False
        assert attrs["first_failure_activity_type"] == "fetch"
        assert attrs["first_failure_error_type"] == "internal"
        assert attrs["bottleneck_activity_type"] == "transform"
        assert attrs["bottleneck_duration_ms"] == 100.0

    def test_summary_no_activities_omits_optional_fields(self):
        inbound = self._make_inbound()
        attrs = inbound._build_summary_attrs(
            common={"app_vitals": "true"},
            info=self._make_info(),
            wf_status="succeeded",
            wf_duration_ms=50.0,
        )
        assert attrs["total_activities"] == 0
        # no preflight detected -> key omitted
        assert "preflight_passed" not in attrs
        # no failures or bottleneck
        assert "first_failure_activity_type" not in attrs
        assert "bottleneck_activity_type" not in attrs


# ───────────────────────────────────────────────────────────────────────────
# _AppVitalsWorkflowInboundInterceptor.execute_workflow
# ───────────────────────────────────────────────────────────────────────────


class TestExecuteWorkflow:
    def _make_inbound(self):
        next_ = mock.MagicMock()
        next_.execute_workflow = mock.AsyncMock(return_value="result")
        inbound = av._AppVitalsWorkflowInboundInterceptor(next_)
        return inbound, next_

    @pytest.mark.asyncio
    async def test_replay_skips_observability(self):
        inbound, next_ = self._make_inbound()
        with mock.patch(
            "application_sdk.observability.app_vitals.workflow.unsafe.is_replaying",
            return_value=True,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as emit:
                result = await inbound.execute_workflow(mock.MagicMock())
        assert result == "result"
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_emits_started_completed_summary(self):
        inbound, next_ = self._make_inbound()
        info = SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=None,
            continued_run_id=None,
            cron_schedule="",
            execution_timeout=None,
            get_current_history_length=lambda: 5,
        )
        with mock.patch(
            "application_sdk.observability.app_vitals.workflow.unsafe.is_replaying",
            return_value=False,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.workflow.info",
                return_value=info,
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals._build_common_attrs",
                    return_value={"app_vitals": "true"},
                ):
                    with mock.patch(
                        "application_sdk.observability.app_vitals._emit_log_event"
                    ) as emit:
                        result = await inbound.execute_workflow(mock.MagicMock())
        assert result == "result"
        # started + completed + summary
        names = [c.args[0] for c in emit.call_args_list]
        assert "app_vitals.wf.started" in names
        assert "app_vitals.wf.completed" in names
        assert "app_vitals.wf.summary" in names

    @pytest.mark.asyncio
    async def test_failure_propagates_and_emits_error_attrs(self):
        next_ = mock.MagicMock()
        next_.execute_workflow = mock.AsyncMock(side_effect=ValueError("boom"))
        inbound = av._AppVitalsWorkflowInboundInterceptor(next_)
        info = SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=None,
            continued_run_id=None,
            cron_schedule="",
            execution_timeout=timedelta(seconds=10),
            get_current_history_length=lambda: 5,
        )
        with mock.patch(
            "application_sdk.observability.app_vitals.workflow.unsafe.is_replaying",
            return_value=False,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.workflow.info",
                return_value=info,
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals._build_common_attrs",
                    return_value={"app_vitals": "true"},
                ):
                    with mock.patch(
                        "application_sdk.observability.app_vitals._emit_log_event"
                    ) as emit:
                        with pytest.raises(ValueError):
                            await inbound.execute_workflow(mock.MagicMock())
        # find completed event and assert it carries error metadata
        completed_calls = [
            c for c in emit.call_args_list if c.args[0] == "app_vitals.wf.completed"
        ]
        assert completed_calls, "wf.completed was not emitted on failure"
        attrs = completed_calls[0].args[1]
        assert attrs["status"] == "failed"
        assert attrs["error_class"] == "ValueError"
        assert "error_fingerprint" in attrs
        assert attrs["wf_timeout_budget_total_ms"] == 10000.0
        assert attrs["history_length"] == 5

    @pytest.mark.asyncio
    async def test_workflow_info_unavailable_in_finally_skips_completion(self):
        """Covers the fallback logger path when workflow.info() fails in cleanup."""
        next_ = mock.MagicMock()
        next_.execute_workflow = mock.AsyncMock(return_value="ok")
        inbound = av._AppVitalsWorkflowInboundInterceptor(next_)
        # First workflow.info() call (for started) succeeds, second (in finally)
        # raises. We cover both branches by using side_effect.
        info_started = SimpleNamespace(
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="WF",
            task_queue="q",
            namespace="ns",
            parent=None,
            continued_run_id=None,
            cron_schedule="",
        )
        info_calls = [info_started, RuntimeError("info-broken")]

        def info_side_effect():
            v = info_calls.pop(0)
            if isinstance(v, BaseException):
                raise v
            return v

        with mock.patch(
            "application_sdk.observability.app_vitals.workflow.unsafe.is_replaying",
            return_value=False,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.workflow.info",
                side_effect=info_side_effect,
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals._build_common_attrs",
                    return_value={"app_vitals": "true"},
                ):
                    with mock.patch(
                        "application_sdk.observability.logger_adaptor.get_logger"
                    ) as gl:
                        gl.return_value = mock.MagicMock()
                        with mock.patch(
                            "application_sdk.observability.app_vitals._emit_log_event"
                        ) as emit:
                            result = await inbound.execute_workflow(mock.MagicMock())
        assert result == "ok"
        # started should fire; completion emission skipped because info failed
        names = [c.args[0] for c in emit.call_args_list]
        assert "app_vitals.wf.started" in names
        assert "app_vitals.wf.completed" not in names


# ───────────────────────────────────────────────────────────────────────────
# _AppVitalsActivityInboundInterceptor.execute_activity
# ───────────────────────────────────────────────────────────────────────────


class TestExecuteActivity:
    def _make_info(self, **overrides):
        defaults = {
            "workflow_id": "wf-1",
            "workflow_run_id": "run-1",
            "activity_id": "act-1",
            "activity_type": "fetch",
            "task_queue": "q",
            "attempt": 1,
            "namespace": "ns",
            "started_time": None,
            "scheduled_time": None,
            "schedule_to_close_timeout": None,
            "start_to_close_timeout": None,
            "retry_policy": None,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_activity_info_failure_skips_emission_and_runs(self):
        """Covers the fallback logger path when activity.info() lookup fails."""
        next_ = mock.MagicMock()
        next_.execute_activity = mock.AsyncMock(return_value="ok")
        inbound = av._AppVitalsActivityInboundInterceptor(next_)
        with mock.patch(
            "application_sdk.observability.app_vitals.activity.info",
            side_effect=RuntimeError("no activity context"),
        ):
            with mock.patch(
                "application_sdk.observability.logger_adaptor.get_logger"
            ) as gl:
                gl.return_value = mock.MagicMock()
                with mock.patch(
                    "application_sdk.observability.app_vitals._emit_log_event"
                ) as emit:
                    result = await inbound.execute_activity(mock.MagicMock())
        assert result == "ok"
        # Exception path returns BEFORE emission.
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_activity_success_emits_started_and_completed(self):
        next_ = mock.MagicMock()
        next_.execute_activity = mock.AsyncMock(
            return_value=SimpleNamespace(assets_processed=42)
        )
        inbound = av._AppVitalsActivityInboundInterceptor(next_)
        info = self._make_info()
        with mock.patch(
            "application_sdk.observability.app_vitals.activity.info",
            return_value=info,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.sample",
                return_value=None,
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals._build_common_attrs",
                    return_value={"app_vitals": "true"},
                ):
                    with mock.patch(
                        "application_sdk.observability.app_vitals._emit_log_event"
                    ) as emit:
                        input_obj = mock.MagicMock()
                        input_obj.args = ()
                        result = await inbound.execute_activity(input_obj)
        assert isinstance(result, SimpleNamespace)
        names = [c.args[0] for c in emit.call_args_list]
        assert "app_vitals.act.started" in names
        assert "app_vitals.act.completed" in names
        # completed event must carry assets_processed
        completed = [
            c for c in emit.call_args_list if c.args[0] == "app_vitals.act.completed"
        ][0].args[1]
        assert completed["assets_processed"] == 42
        assert completed["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_activity_failure_records_error_and_reraises(self):
        next_ = mock.MagicMock()
        next_.execute_activity = mock.AsyncMock(side_effect=RuntimeError("boom"))
        inbound = av._AppVitalsActivityInboundInterceptor(next_)
        info = self._make_info(
            schedule_to_close_timeout=timedelta(seconds=20),
            retry_policy=SimpleNamespace(maximum_attempts=3),
        )
        with mock.patch(
            "application_sdk.observability.app_vitals.activity.info",
            return_value=info,
        ):
            with mock.patch(
                "application_sdk.observability.app_vitals.sample",
                return_value=None,
            ):
                with mock.patch(
                    "application_sdk.observability.app_vitals._build_common_attrs",
                    return_value={"app_vitals": "true"},
                ):
                    with mock.patch(
                        "application_sdk.observability.app_vitals._emit_log_event"
                    ) as emit:
                        input_obj = mock.MagicMock()
                        input_obj.args = ()
                        with pytest.raises(RuntimeError):
                            await inbound.execute_activity(input_obj)
        completed = [
            c for c in emit.call_args_list if c.args[0] == "app_vitals.act.completed"
        ][0].args[1]
        assert completed["status"] == "failed"
        assert completed["error_class"] == "RuntimeError"
        assert "error_fingerprint" in completed
        assert completed["timeout_budget_total_ms"] == 20000.0
        assert completed["retry_max_attempts"] == 3


# ───────────────────────────────────────────────────────────────────────────
# Outbound interceptor — start_activity wraps awaitable; child workflow flow
# ───────────────────────────────────────────────────────────────────────────


class TestOutboundInterceptor:
    def _make_pair(self):
        next_inbound = mock.MagicMock()
        inbound = av._AppVitalsWorkflowInboundInterceptor(next_inbound)
        next_outbound = mock.MagicMock()
        outbound = av._AppVitalsWorkflowOutboundInterceptor(next_outbound, inbound)
        return inbound, outbound, next_outbound

    def test_start_activity_appends_record_and_returns_tracked_awaitable(self):
        inbound, outbound, next_outbound = self._make_pair()

        async def coro():
            return "v"

        next_outbound.start_activity.return_value = coro()
        input_obj = mock.MagicMock()
        input_obj.activity = "fetch"
        result = outbound.start_activity(input_obj)
        # one record appended, status pending
        assert len(inbound._activity_records) == 1
        assert inbound._activity_records[0]["activity_type"] == "fetch"
        assert inbound._activity_records[0]["status"] == "pending"
        # result is a coroutine (the tracker)
        assert hasattr(result, "__await__")
        # close to avoid coroutine-was-never-awaited warning
        result.close()

    @pytest.mark.asyncio
    async def test_start_child_workflow_success_path(self):
        inbound, outbound, next_outbound = self._make_pair()
        next_outbound.start_child_workflow = mock.AsyncMock(return_value="handle")
        input_obj = mock.MagicMock()
        input_obj.workflow = "ChildWF"
        out = await outbound.start_child_workflow(input_obj)
        assert out == "handle"
        rec = inbound._child_workflow_records[0]
        assert rec["status"] == "started"
        assert rec["child_workflow_type"] == "ChildWF"

    @pytest.mark.asyncio
    async def test_start_child_workflow_failure_records_error_and_reraises(self):
        inbound, outbound, next_outbound = self._make_pair()
        next_outbound.start_child_workflow = mock.AsyncMock(
            side_effect=RuntimeError("nope")
        )
        input_obj = mock.MagicMock()
        input_obj.workflow = "ChildWF"
        with mock.patch(
            "application_sdk.observability.app_vitals.classify_error",
            return_value="internal",
        ):
            with pytest.raises(RuntimeError):
                await outbound.start_child_workflow(input_obj)
        rec = inbound._child_workflow_records[0]
        assert rec["status"] == "failed"
        assert rec["error_type"] == "internal"

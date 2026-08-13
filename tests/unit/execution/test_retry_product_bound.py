"""Unit tests for the bound on a task's retry product (FND-294, ADR-0018).

``start_to_close`` bounds one attempt; the retry policy multiplies it. These
tests pin the mechanism that makes the *product* boundable — and pin that the
default still leaves it unbounded, because ADR-0018's accepted position is to
start there deliberately and size the ceiling from warn-mode data.

The dispatch assertions run through ``_create_task_activity_wrapper`` and
``get_activity_options`` — the two code paths that actually hand timeouts to
Temporal — rather than against the resolver alone, so a ceiling that resolves
correctly but never reaches the wire still fails.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import (
    TaskContractError,
    _load_default_schedule_to_close_seconds,
    get_task_metadata,
    task,
)
from application_sdk.contracts.base import Input, Output
from application_sdk.execution import retry_product_seconds
from application_sdk.execution._temporal.activities import get_activity_options
from application_sdk.execution.retry import resolve_activity_time_bounds

_EVICTION_RETRY_PATH = (
    "application_sdk.execution._temporal.eviction_retry."
    "execute_activity_with_eviction_retry"
)


class _In(Input):
    pass


class _Out(Output):
    pass


async def _dispatch_kwargs(**wrapper_kwargs: object) -> dict[str, object]:
    """Kwargs the workflow-side wrapper hands to Temporal for one dispatch."""
    with patch(_EVICTION_RETRY_PATH, new_callable=AsyncMock) as mock_exec:
        from application_sdk.app.base import _create_task_activity_wrapper

        mock_exec.return_value = MagicMock()
        wrapper = _create_task_activity_wrapper(
            app_name="qi-app",
            task_name="extract",
            output_type=_Out,
            context_data={"run_id": "r1", "correlation_id": "c1"},
            retry_max_attempts=3,
            retry_max_interval_seconds=30,
            **wrapper_kwargs,  # type: ignore[arg-type]
        )
        await wrapper(MagicMock())

    return dict(mock_exec.call_args.kwargs)


# ---------------------------------------------------------------------------
# The arithmetic: what a per-attempt ceiling costs once retries multiply it
# ---------------------------------------------------------------------------


class TestRetryProductSeconds:
    def test_product_is_attempts_times_one_attempt_plus_backoff_headroom(self) -> None:
        assert retry_product_seconds(3600, 3) == 3 * 3600 + 10

    def test_the_24h_backstop_at_three_attempts_is_the_72h_worst_case(self) -> None:
        """The number ADR-0018 accepts as the starting point, in one assertion."""
        assert retry_product_seconds(86_400, 3) == 72 * 3600 + 10

    def test_headroom_leaves_room_for_the_backoff_between_attempts(self) -> None:
        """A ceiling of exactly attempts x attempt would fire during the last
        backoff wait, silently costing an attempt the retry policy grants."""
        assert retry_product_seconds(600, 2) > 2 * 600

    def test_zero_and_negative_attempts_still_bound_one_attempt(self) -> None:
        """One attempt always runs, whatever a malformed policy says."""
        assert retry_product_seconds(600, 0) == 610
        assert retry_product_seconds(600, -5) == 610


# ---------------------------------------------------------------------------
# Resolution: the one reader both dispatch paths share
# ---------------------------------------------------------------------------


class TestResolveActivityTimeBounds:
    def test_no_ceiling_leaves_the_product_unbounded(self) -> None:
        assert resolve_activity_time_bounds(600, None) == (600, None)

    def test_a_ceiling_above_one_attempt_bounds_the_product_only(self) -> None:
        assert resolve_activity_time_bounds(600, 1810) == (600, 1810)

    def test_a_ceiling_below_one_attempt_caps_that_attempt_too(self) -> None:
        """Declaring only a total is the shape an app gets while inheriting a
        generous per-attempt backstop; the tighter number is the declaration."""
        assert resolve_activity_time_bounds(86_400, 3600) == (3600, 3600)

    def test_never_returns_an_inverted_pair(self) -> None:
        start_to_close, schedule_to_close = resolve_activity_time_bounds(86_400, 60)
        assert schedule_to_close is not None
        assert start_to_close <= schedule_to_close


# ---------------------------------------------------------------------------
# The declaration surface: @task, and the fleet-wide env var
# ---------------------------------------------------------------------------


class TestTaskDeclaration:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_unset_by_default_so_todays_behaviour_is_unchanged(self) -> None:
        class _App(App):
            @task(timeout_seconds=600)
            async def extract(self, input: _In) -> _Out:
                return _Out()

            async def run(self, input: _In) -> _Out:
                return _Out()

        metadata = get_task_metadata(_App.extract)
        assert metadata is not None
        assert metadata.schedule_to_close_seconds is None

    def test_explicit_ceiling_is_carried_on_the_metadata(self) -> None:
        class _App(App):
            @task(timeout_seconds=600, schedule_to_close_seconds=1810)
            async def extract(self, input: _In) -> _Out:
                return _Out()

            async def run(self, input: _In) -> _Out:
                return _Out()

        metadata = get_task_metadata(_App.extract)
        assert metadata is not None
        assert metadata.schedule_to_close_seconds == 1810

    def test_zero_is_refused_rather_than_read_as_disabled(self) -> None:
        """``0`` would fail every attempt before it started. ``None`` is how a
        task opts out; the error has to say so."""
        with pytest.raises(TaskContractError, match="Pass None"):

            @task(schedule_to_close_seconds=0)
            async def extract(input: _In) -> _Out:
                return _Out()

    def test_negative_is_refused(self) -> None:
        with pytest.raises(TaskContractError, match="positive number of seconds"):

            @task(schedule_to_close_seconds=-1)
            async def extract(input: _In) -> _Out:
                return _Out()


class TestFleetWideEnvVar:
    """The decision after warn-mode data is a config change, so the env var is
    the surface that matters most — including its failure modes, which must cost
    the config value and never the worker's boot."""

    def test_a_positive_value_becomes_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", "7200")
        assert _load_default_schedule_to_close_seconds() == 7200

    def test_unset_leaves_the_product_unbounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", raising=False)
        assert _load_default_schedule_to_close_seconds() is None

    def test_zero_clears_an_inherited_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", "0")
        assert _load_default_schedule_to_close_seconds() is None

    def test_a_negative_value_is_ignored_rather_than_failing_every_decoration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", "-30")
        assert _load_default_schedule_to_close_seconds() is None

    def test_a_malformed_value_is_ignored_rather_than_failing_every_decoration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", "sometimes")
        assert _load_default_schedule_to_close_seconds() is None


# ---------------------------------------------------------------------------
# The wire: both paths that hand timeouts to Temporal
# ---------------------------------------------------------------------------


class TestGetActivityOptions:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _options(self, **task_kwargs: object) -> dict[str, object]:
        class _OptionsApp(App):
            @task(**task_kwargs)  # type: ignore[arg-type]
            async def extract(self, input: _In) -> _Out:
                return _Out()

            async def run(self, input: _In) -> _Out:
                return _Out()

        tasks = TaskRegistry.get_instance().get_tasks_for_app("_options-app")
        return get_activity_options(next(t for t in tasks if t.name == "extract"))

    def test_no_schedule_to_close_without_a_declared_ceiling(self) -> None:
        options = self._options(timeout_seconds=600)

        assert "schedule_to_close_timeout" not in options
        assert options["start_to_close_timeout"] == timedelta(seconds=600)

    def test_a_declared_ceiling_reaches_the_options(self) -> None:
        options = self._options(timeout_seconds=600, schedule_to_close_seconds=1810)

        assert options["schedule_to_close_timeout"] == timedelta(seconds=1810)
        assert options["start_to_close_timeout"] == timedelta(seconds=600)

    def test_a_ceiling_below_one_attempt_caps_start_to_close_in_the_options(
        self,
    ) -> None:
        options = self._options(timeout_seconds=86_400, schedule_to_close_seconds=3600)

        assert options["start_to_close_timeout"] == timedelta(seconds=3600)
        assert options["schedule_to_close_timeout"] == timedelta(seconds=3600)


class TestWorkflowDispatch:
    async def test_no_schedule_to_close_kwarg_without_a_declared_ceiling(self) -> None:
        """Byte-identical to before this knob existed for every task that has
        not opted in — the kwarg is omitted, not passed as None."""
        kwargs = await _dispatch_kwargs(timeout_seconds=600)

        assert "schedule_to_close_timeout" not in kwargs
        assert kwargs["start_to_close_timeout"] == timedelta(seconds=600)

    async def test_a_declared_ceiling_reaches_the_dispatch(self) -> None:
        kwargs = await _dispatch_kwargs(
            timeout_seconds=600, schedule_to_close_seconds=1810
        )

        assert kwargs["schedule_to_close_timeout"] == timedelta(seconds=1810)
        assert kwargs["start_to_close_timeout"] == timedelta(seconds=600)

    async def test_a_ceiling_below_one_attempt_caps_the_dispatched_start_to_close(
        self,
    ) -> None:
        kwargs = await _dispatch_kwargs(
            timeout_seconds=86_400, schedule_to_close_seconds=3600
        )

        assert kwargs["start_to_close_timeout"] == timedelta(seconds=3600)
        assert kwargs["schedule_to_close_timeout"] == timedelta(seconds=3600)

    async def test_bounding_at_the_retry_product_changes_nothing_about_one_attempt(
        self,
    ) -> None:
        """The ceiling that makes today's worst case explicit and enforced."""
        kwargs = await _dispatch_kwargs(
            timeout_seconds=3600,
            schedule_to_close_seconds=retry_product_seconds(3600, 3),
        )

        assert kwargs["start_to_close_timeout"] == timedelta(seconds=3600)
        assert kwargs["schedule_to_close_timeout"] == timedelta(seconds=3 * 3600 + 10)

"""Tests for the @task decorator and TaskMetadata."""

from dataclasses import dataclass

import pytest

from application_sdk.app.task import (
    TaskContractError,
    TaskMetadata,
    get_task_metadata,
    is_task,
    task,
)
from application_sdk.contracts.base import Input, Output

# =============================================================================
# Test fixtures
# =============================================================================


@dataclass
class SimpleInput(Input):
    value: str = ""


@dataclass
class SimpleOutput(Output):
    result: str = ""


# =============================================================================
# @task decorator - basic usage
# =============================================================================


class TestTaskDecoratorBasicUsage:
    """Tests for @task decorator syntax variants."""

    def test_task_without_parens(self) -> None:
        """@task without parens works."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        assert is_task(MyApp.my_task)

    def test_task_with_empty_parens(self) -> None:
        """@task() with parens works."""

        class MyApp:
            @task()
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        assert is_task(MyApp.my_task)

    def test_task_with_timeout(self) -> None:
        """@task(timeout_seconds=300) sets timeout."""

        class MyApp:
            @task(timeout_seconds=300)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.timeout_seconds == 300

    def test_task_with_name_override(self) -> None:
        """@task(name='custom') overrides the default name."""

        class MyApp:
            @task(name="custom-name")
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.name == "custom-name"

    def test_task_default_name_is_function_name(self) -> None:
        """Default task name is function name."""

        class MyApp:
            @task
            async def fetch_data(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.fetch_data)
        assert metadata is not None
        assert metadata.name == "fetch_data"


# =============================================================================
# @task decorator - metadata
# =============================================================================


class TestTaskMetadata:
    """Tests for TaskMetadata content."""

    def test_task_metadata_has_correct_fields(self) -> None:
        """TaskMetadata has correct fields."""

        class MyApp:
            @task(timeout_seconds=120, retry_max_attempts=5)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert isinstance(metadata, TaskMetadata)
        assert metadata.name == "my_task"
        assert metadata.timeout_seconds == 120
        assert metadata.retry_max_attempts == 5

    def test_task_metadata_input_type(self) -> None:
        """TaskMetadata records input_type correctly."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.input_type is SimpleInput

    def test_task_metadata_output_type(self) -> None:
        """TaskMetadata records output_type correctly."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.output_type is SimpleOutput

    def test_task_metadata_app_name_initially_empty(self) -> None:
        """TaskMetadata app_name is empty until set by App registration."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.app_name == ""

    def test_task_default_heartbeat_settings(self) -> None:
        """Default heartbeat settings are correct."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds == 60
        assert metadata.auto_heartbeat_seconds == 10

    def test_task_custom_heartbeat_settings(self) -> None:
        """Custom heartbeat settings are preserved."""

        class MyApp:
            @task(heartbeat_timeout_seconds=120, auto_heartbeat_seconds=20)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds == 120
        assert metadata.auto_heartbeat_seconds == 20

    def test_task_disable_heartbeat(self) -> None:
        """Setting heartbeat_timeout_seconds=None disables heartbeating."""

        class MyApp:
            @task(heartbeat_timeout_seconds=None)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds is None


# =============================================================================
# @task decorator - contract validation
# =============================================================================


class TestTaskContractValidation:
    """Tests for task contract enforcement."""

    def test_task_contract_error_no_params(self) -> None:
        """TaskContractError raised when task has no params."""
        with pytest.raises(TaskContractError, match="must have exactly one parameter"):

            class MyApp:
                @task
                async def my_task(self) -> SimpleOutput:
                    return SimpleOutput()

    def test_task_contract_error_too_many_params(self) -> None:
        """TaskContractError raised when task has more than one param."""
        with pytest.raises(TaskContractError, match="must have exactly one parameter"):

            class MyApp:
                @task
                async def my_task(
                    self, input1: SimpleInput, input2: SimpleInput
                ) -> SimpleOutput:
                    return SimpleOutput()

    def test_task_contract_error_wrong_input_type(self) -> None:
        """TaskContractError raised when input does not extend Input."""
        with pytest.raises(TaskContractError, match="must extend Input base class"):

            class MyApp:
                @task
                async def my_task(self, input: str) -> SimpleOutput:
                    return SimpleOutput()

    def test_task_contract_error_no_return_annotation(self) -> None:
        """TaskContractError raised when return type is missing."""
        with pytest.raises(
            TaskContractError, match="must have a return type annotation"
        ):

            class MyApp:
                @task
                async def my_task(self, input: SimpleInput):  # type: ignore[return]
                    return SimpleOutput()

    def test_task_contract_error_wrong_return_type(self) -> None:
        """TaskContractError raised when return type does not extend Output."""
        with pytest.raises(TaskContractError, match="must extend Output base class"):

            class MyApp:
                @task
                async def my_task(self, input: SimpleInput) -> str:  # type: ignore[return-value]
                    return "result"


# =============================================================================
# is_task / get_task_metadata helpers
# =============================================================================


class TestTaskHelpers:
    """Tests for is_task() and get_task_metadata() helpers."""

    def test_is_task_returns_true_for_decorated(self) -> None:
        """is_task() returns True for decorated function."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert is_task(MyApp.my_task) is True

    def test_is_task_returns_false_for_plain_function(self) -> None:
        """is_task() returns False for non-decorated function."""

        async def plain_fn() -> None:
            pass

        assert is_task(plain_fn) is False

    def test_is_task_returns_false_for_non_callable(self) -> None:
        """is_task() returns False for non-callable."""
        assert is_task(42) is False
        assert is_task("string") is False
        assert is_task(None) is False

    def test_get_task_metadata_returns_metadata(self) -> None:
        """get_task_metadata() returns TaskMetadata for decorated function."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert isinstance(metadata, TaskMetadata)

    def test_get_task_metadata_returns_none_for_plain(self) -> None:
        """get_task_metadata() returns None for non-decorated function."""

        async def plain_fn() -> None:
            pass

        assert get_task_metadata(plain_fn) is None


# =============================================================================
# @task with retry policy
# =============================================================================


class TestTaskRetryPolicy:
    """Tests for @task with retry_policy parameter."""

    def test_task_with_retry_policy(self) -> None:
        """@task can accept a RetryPolicy."""
        from application_sdk.execution.retry import RetryPolicy

        policy = RetryPolicy(max_attempts=5)

        class MyApp:
            @task(retry_policy=policy)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.retry_policy is policy

    def test_task_without_retry_policy_is_none(self) -> None:
        """Default retry_policy is None (uses retry_max_attempts instead)."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.retry_policy is None


# =============================================================================
# Env-var-driven defaults
# =============================================================================


class TestTaskEnvVarDefaults:
    """Tests for env-var-driven timeout defaults."""

    @staticmethod
    def _task_module():
        import sys

        # application_sdk/app/__init__.py re-exports `task` as an attribute,
        # so `import application_sdk.app.task as m` gives the function, not the
        # module. sys.modules always holds the real module object.
        return sys.modules["application_sdk.app.task"]

    def test_heartbeat_timeout_from_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ATLAN_HEARTBEAT_TIMEOUT_SECONDS overrides heartbeat_timeout_seconds default."""
        monkeypatch.setattr(
            self._task_module(), "_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS", 300
        )

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds == 300

    def test_start_to_close_timeout_from_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS overrides timeout_seconds default."""
        monkeypatch.setattr(self._task_module(), "_DEFAULT_TIMEOUT_SECONDS", 1800)

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.timeout_seconds == 1800

    def test_explicit_task_param_overrides_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit @task parameter takes precedence over env-var default."""
        monkeypatch.setattr(
            self._task_module(), "_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS", 300
        )

        class MyApp:
            @task(heartbeat_timeout_seconds=120)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.heartbeat_timeout_seconds == 120

    def test_invalid_env_var_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-integer env var value falls back to the hardcoded default."""
        from application_sdk.common._env import env_int

        monkeypatch.setenv("ATLAN_HEARTBEAT_TIMEOUT_SECONDS", "not-a-number")
        result = env_int("ATLAN_HEARTBEAT_TIMEOUT_SECONDS", 60)
        assert result == 60


# =============================================================================
# @task stall watchdog (ADR-0018 / FND-296)
# =============================================================================


class TestTaskDurationBackstop:
    """``timeout_seconds`` stops being a duration budget and becomes a backstop."""

    def test_the_default_is_the_24h_backstop(self) -> None:
        """The relief half of FND-296, and the one thing that changes on upgrade.

        It ships in the same release as warn mode on purpose: coupling it to
        ``enforce`` would leave every app dying at its guessed ceiling while the
        fleet gathered data, so "no upgrade task" would buy nobody anything
        (ADR-0018 → *Migration*).
        """
        from application_sdk.app.task import _START_TO_CLOSE_BACKSTOP_SECONDS

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.timeout_seconds == _START_TO_CLOSE_BACKSTOP_SECONDS == 86_400

    def test_an_app_that_still_declares_one_keeps_it(self) -> None:
        """Deleting the guesses is an optimisation, not a migration step."""

        class MyApp:
            @task(timeout_seconds=7200)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.timeout_seconds == 7200


class TestTaskProgressWatchdog:
    """``@task(progress_watchdog=..., max_no_progress_seconds=...)``."""

    def test_declaring_nothing_stores_nothing(self) -> None:
        """``None``, not ``WARN``.

        "Declares nothing" and "declares warn" are different facts: only the
        first one follows an operator moving the fleet, and only the first one
        yields to the ``off`` kill-switch without argument.
        """

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.progress_watchdog is None
        assert metadata.max_no_progress_seconds is None

    @pytest.mark.parametrize("declared", ["off", "warn", "enforce"])
    def test_a_string_mode_is_coerced_to_the_enum(self, declared: str) -> None:
        """The enum is a ``StrEnum``, so the string form is the ergonomic one."""
        from application_sdk.execution.progress import ProgressWatchdogMode

        class MyApp:
            @task(progress_watchdog=declared)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.progress_watchdog is ProgressWatchdogMode(declared)

    def test_the_enum_is_accepted_directly(self) -> None:
        from application_sdk.execution.progress import ProgressWatchdogMode

        class MyApp:
            @task(progress_watchdog=ProgressWatchdogMode.ENFORCE)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.progress_watchdog is ProgressWatchdogMode.ENFORCE

    def test_an_unknown_mode_is_rejected_at_decoration(self) -> None:
        """Loud at import, unlike the env-var reader next door.

        A typo in a deployment manifest must not stop a worker booting; a typo in
        an app's own source is a bug its author should see immediately.
        """
        with pytest.raises(TaskContractError, match="not a valid mode"):

            class MyApp:
                @task(progress_watchdog="enfroce")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_a_declared_allowance_is_stored_as_seconds(self) -> None:
        class MyApp:
            @task(max_no_progress_seconds=1800)
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.max_no_progress_seconds == 1800.0

    @pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
    def test_an_unusable_allowance_is_rejected(self, bad: float) -> None:
        """Zero stalls every attempt on its first tick; NaN slips past ``<= 0``.

        In an enforcing task either one is a kill switch wearing a config knob's
        clothes, so neither is allowed to reach a decoration.
        """
        with pytest.raises(TaskContractError, match="finite positive"):

            class MyApp:
                @task(max_no_progress_seconds=bad)
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()


# =============================================================================
# @task pool field
# =============================================================================


class TestTaskPool:
    """Tests for @task(pool=...) — logical worker-pool routing."""

    def test_task_without_pool_has_none(self) -> None:
        """Tasks without pool default to None."""

        class MyApp:
            @task
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.pool is None

    def test_task_with_pool_stores_it(self) -> None:
        """@task(pool='main') stores the pool string."""

        class MyApp:
            @task(pool="main")
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.pool == "main"

    def test_task_pool_empty_parens_is_none(self) -> None:
        """@task() without pool= still defaults to None."""

        class MyApp:
            @task()
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.pool is None

    def test_pool_queue_threaded_to_execute_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wrapper passes task_queue='qi-app-queue-heavy' for pool='heavy'.

        This is the definitive end-to-end check: it calls through
        _create_task_activity_wrapper (not a re-implementation) so it catches
        regressions in resolution order, closure capture, .upper() casing, and
        kwarg-threading shape in one shot.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import _create_task_activity_wrapper

        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "qi-app-queue")

        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="qi-app",
                task_name="analyse-heavy",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=SimpleOutput,
                context_data={"run_id": "r1", "correlation_id": "c1"},
                pool="heavy",
            )
            import asyncio

            asyncio.run(wrapper(MagicMock()))

        assert mock_exec.call_args.kwargs["task_queue"] == "qi-app-queue-heavy"

    def test_pool_explicit_env_var_overrides_derived_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit ATLAN_POOL_<POOL>_QUEUE takes precedence over derived queue."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import _create_task_activity_wrapper

        monkeypatch.setenv("ATLAN_POOL_HEAVY_QUEUE", "custom-heavy-queue")
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "qi-app-queue")

        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="qi-app",
                task_name="analyse-heavy",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=SimpleOutput,
                context_data={"run_id": "r1", "correlation_id": "c1"},
                pool="heavy",
            )
            import asyncio

            asyncio.run(wrapper(MagicMock()))

        assert mock_exec.call_args.kwargs["task_queue"] == "custom-heavy-queue"

    def test_no_pool_no_task_queue_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrapper without pool passes no task_queue kwarg (backward-compatible)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import _create_task_activity_wrapper

        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="qi-app",
                task_name="analyse",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=SimpleOutput,
                context_data={"run_id": "r1", "correlation_id": "c1"},
            )
            import asyncio

            asyncio.run(wrapper(MagicMock()))

        assert "task_queue" not in mock_exec.call_args.kwargs

    def test_pool_uppercase_raises_error(self) -> None:
        """@task(pool='HotPool') raises TaskContractError — pool must be lowercase kebab-case."""
        with pytest.raises(TaskContractError, match="lowercase kebab-case"):

            class MyApp:
                @task(pool="HotPool")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_pool_empty_string_raises_error(self) -> None:
        """@task(pool='') raises TaskContractError."""
        with pytest.raises(TaskContractError, match="empty or whitespace"):

            class MyApp:
                @task(pool="")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_pool_whitespace_only_raises_error(self) -> None:
        """@task(pool='  ') raises TaskContractError."""
        with pytest.raises(TaskContractError, match="empty or whitespace"):

            class MyApp:
                @task(pool="  ")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_pool_trailing_dash_raises_error(self) -> None:
        """@task(pool='pool-') raises TaskContractError — trailing dash is not kebab-case."""
        with pytest.raises(TaskContractError, match="lowercase kebab-case"):

            class MyApp:
                @task(pool="pool-")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_pool_consecutive_dashes_raises_error(self) -> None:
        """@task(pool='pool--name') raises TaskContractError — consecutive dashes rejected."""
        with pytest.raises(TaskContractError, match="lowercase kebab-case"):

            class MyApp:
                @task(pool="pool--name")
                async def my_task(self, input: SimpleInput) -> SimpleOutput:
                    return SimpleOutput()

    def test_pool_valid_kebab_with_hyphen_accepted(self) -> None:
        """@task(pool='cold-tier') is valid kebab-case."""

        class MyApp:
            @task(pool="cold-tier")
            async def my_task(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_task_metadata(MyApp.my_task)
        assert metadata is not None
        assert metadata.pool == "cold-tier"

    def test_pool_hyphen_normalised_to_underscore_in_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pool='cold-tier' resolves ATLAN_POOL_COLD_TIER_QUEUE, not ATLAN_POOL_COLD-TIER_QUEUE."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import _create_task_activity_wrapper

        monkeypatch.setenv("ATLAN_POOL_COLD_TIER_QUEUE", "explicit-cold-queue")
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "base-queue")

        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="qi-app",
                task_name="cold-export",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=SimpleOutput,
                context_data={"run_id": "r1", "correlation_id": "c1"},
                pool="cold-tier",
            )
            import asyncio

            asyncio.run(wrapper(MagicMock()))

        assert mock_exec.call_args.kwargs["task_queue"] == "explicit-cold-queue"


class TestTaskPoolChartEnv:
    """Pool routing under the env the `atlan-app` chart actually sets.

    The chart sets neither ``ATLAN_POOL_<POOL>_QUEUE`` nor ``ATLAN_TASK_QUEUE``.
    It sets ``ATLAN_APPLICATION_NAME`` + ``ATLAN_DEPLOYMENT_NAME``, and renders a
    pool's queue as ``atlan-<app>-<deployment>-<pool>``. Before the
    ``task_queue_from_env`` fallback, every chart-deployed ``@task(pool=...)``
    resolved to ``None`` and its activities silently ran on the workflow's
    default queue — the dedicated pool idle while the main worker absorbed the
    load it existed to offload.
    """

    @staticmethod
    def _chart_env(monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly the queue-relevant env a chart-deployed pool pod receives."""
        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "automation-engine")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "default")

    def test_pool_queue_matches_the_chart_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Derived queue is byte-identical to the chart's own formula.

        The chart renders ``atlan-%s-%s-%s`` from name/deploymentName/pool
        (``subcharts/atlan-app/templates/deployment.yaml``). Pinning the literal
        here is the point: if either side's formula changes, routed activities
        land on a queue no worker polls, and that is invisible at deploy time.
        """
        from application_sdk.app.registry import resolve_pool_queue

        self._chart_env(monkeypatch)
        assert resolve_pool_queue("heavy") == "atlan-automation-engine-default-heavy"

    def test_hyphenated_pool_name_survives_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kebab-case pool key is appended verbatim, not underscore-normalised.

        Underscore normalisation applies only to the env-var KEY lookup; the
        queue itself keeps the hyphen so it matches the chart, which interpolates
        the pool name straight into the queue string.
        """
        from application_sdk.app.registry import resolve_pool_queue

        self._chart_env(monkeypatch)
        monkeypatch.delenv("ATLAN_POOL_COLD_TIER_QUEUE", raising=False)
        assert (
            resolve_pool_queue("cold-tier")
            == "atlan-automation-engine-default-cold-tier"
        )

    def test_explicit_env_still_outranks_the_chart_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new fallback is last, so it cannot shadow an explicit override."""
        from application_sdk.app.registry import resolve_pool_queue

        self._chart_env(monkeypatch)
        monkeypatch.setenv("ATLAN_POOL_HEAVY_QUEUE", "operator-pinned-queue")
        assert resolve_pool_queue("heavy") == "operator-pinned-queue"

    def test_atlan_task_queue_still_outranks_the_chart_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit base queue keeps winning over the derived one."""
        from application_sdk.app.registry import resolve_pool_queue

        self._chart_env(monkeypatch)
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "local-dev-queue")
        assert resolve_pool_queue("heavy") == "local-dev-queue-heavy"

    def test_no_app_name_still_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing nameable in the env → None, not a manufactured queue.

        ``derive_task_queue`` refuses to invent a queue without an app name, and
        the fallback must not paper over that with a bare ``-heavy``.
        """
        from application_sdk.app.registry import resolve_pool_queue

        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
        assert resolve_pool_queue("heavy") is None

    def test_app_name_without_deployment_drops_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App name alone derives the bare ``{app}`` base, per derive_task_queue."""
        from application_sdk.app.registry import resolve_pool_queue

        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "automation-engine")
        assert resolve_pool_queue("heavy") == "automation-engine-heavy"

    def test_dispatch_path_routes_to_the_chart_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end through the real wrapper, not just the resolver.

        Calls _create_task_activity_wrapper so closure capture and kwarg
        threading are covered too — the resolver being right is not the same as
        the dispatched activity carrying the right queue.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import _create_task_activity_wrapper

        self._chart_env(monkeypatch)

        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="automation-engine",
                task_name="analyse-heavy",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=SimpleOutput,
                context_data={"run_id": "r1", "correlation_id": "c1"},
                pool="heavy",
            )
            import asyncio

            asyncio.run(wrapper(MagicMock()))

        assert (
            mock_exec.call_args.kwargs["task_queue"]
            == "atlan-automation-engine-default-heavy"
        )

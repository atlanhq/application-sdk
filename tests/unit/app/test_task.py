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

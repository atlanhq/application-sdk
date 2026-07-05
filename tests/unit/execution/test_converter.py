"""Unit tests for Temporal data converter configuration."""

from __future__ import annotations

from temporalio.client import Client as _TemporalClientImpl
from temporalio.client import WorkflowFailureError as _TemporalWorkflowFailureErrorImpl
from temporalio.converter import DataConverter
from temporalio.exceptions import ActivityError as _TemporalActivityErrorImpl
from temporalio.exceptions import CancelledError as _TemporalCancelledErrorImpl
from temporalio.exceptions import ChildWorkflowError as _TemporalChildWorkflowErrorImpl
from temporalio.exceptions import TerminatedError as _TemporalTerminatedErrorImpl
from temporalio.exceptions import TimeoutError as _TemporalTimeoutErrorImpl

from application_sdk.contracts.base import Input, Output
from application_sdk.execution import (
    TemporalActivityError,
    TemporalCancelledError,
    TemporalChildWorkflowError,
    TemporalClient,
    TemporalTerminatedError,
    TemporalTimeoutError,
    TemporalWorkflowFailureError,
    create_data_converter_for_app,
)
from application_sdk.execution._temporal.converter import create_data_converter


class _ConverterInput(Input):
    name: str = "test"
    count: int = 0


class _ConverterOutput(Output):
    result: str = ""
    success: bool = True


class TestPublicSurface:
    """Smoke tests for the application_sdk.execution public surface."""

    def test_temporal_client_is_exported(self) -> None:
        assert TemporalClient is _TemporalClientImpl

    def test_temporal_workflow_failure_error_is_exported(self) -> None:
        assert TemporalWorkflowFailureError is _TemporalWorkflowFailureErrorImpl

    def test_temporal_activity_error_is_exported(self) -> None:
        assert TemporalActivityError is _TemporalActivityErrorImpl

    def test_temporal_cancelled_error_is_exported(self) -> None:
        assert TemporalCancelledError is _TemporalCancelledErrorImpl

    def test_temporal_child_workflow_error_is_exported(self) -> None:
        assert TemporalChildWorkflowError is _TemporalChildWorkflowErrorImpl

    def test_temporal_terminated_error_is_exported(self) -> None:
        assert TemporalTerminatedError is _TemporalTerminatedErrorImpl

    def test_temporal_timeout_error_is_exported(self) -> None:
        assert TemporalTimeoutError is _TemporalTimeoutErrorImpl

    def test_temporal_error_reexports_are_in_dunder_all(self) -> None:
        from application_sdk import execution

        assert {
            "TemporalWorkflowFailureError",
            "TemporalActivityError",
            "TemporalCancelledError",
            "TemporalChildWorkflowError",
            "TemporalTerminatedError",
            "TemporalTimeoutError",
        } <= set(execution.__all__)

    def test_create_data_converter_for_app_is_exported(self) -> None:
        from application_sdk.execution._temporal.converter import (
            create_data_converter_for_app as _internal,
        )

        assert create_data_converter_for_app is _internal


class TestCreateDataConverter:
    """Tests for create_data_converter()."""

    def test_returns_data_converter_instance(self) -> None:
        converter = create_data_converter()
        assert isinstance(converter, DataConverter)

    def test_returns_data_converter_without_additional_converters(self) -> None:
        converter = create_data_converter(additional_converters=None)
        assert isinstance(converter, DataConverter)

    def test_returns_data_converter_with_empty_additional_converters(self) -> None:
        converter = create_data_converter(additional_converters=[])
        assert isinstance(converter, DataConverter)

    def test_two_calls_return_independent_instances(self) -> None:
        converter1 = create_data_converter()
        converter2 = create_data_converter()
        # Both are valid DataConverter instances
        assert isinstance(converter1, DataConverter)
        assert isinstance(converter2, DataConverter)


class TestCreateDataConverterForApp:
    """Tests for create_data_converter_for_app()."""

    def test_returns_data_converter_for_app_without_custom_converters(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        try:

            class _SampleApp(App):
                async def run(self, input: _ConverterInput) -> _ConverterOutput:
                    return _ConverterOutput()

            converter = create_data_converter_for_app(_SampleApp)
            assert isinstance(converter, DataConverter)
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()

    def test_returns_data_converter_for_app_with_no_payload_converters_attr(
        self,
    ) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        try:

            class _PlainApp(App):
                async def run(self, input: _ConverterInput) -> _ConverterOutput:
                    return _ConverterOutput()

            # No payload_converters defined
            assert not hasattr(_PlainApp, "payload_converters")
            converter = create_data_converter_for_app(_PlainApp)
            assert isinstance(converter, DataConverter)
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()

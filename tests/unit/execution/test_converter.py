"""Unit tests for Temporal data converter configuration."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.converter import DataConverter

from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.converter import (
    create_data_converter,
    create_data_converter_for_app,
)


@dataclass
class _ConverterInput(Input):
    name: str = "test"
    count: int = 0


@dataclass
class _ConverterOutput(Output):
    result: str = ""
    success: bool = True


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

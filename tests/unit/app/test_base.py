"""Tests for App base class behavior (no Temporal worker needed)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App, AppError, NonRetryableError, _pascal_to_kebab
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.errors import APP_ERROR, APP_NON_RETRYABLE

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
# _pascal_to_kebab
# =============================================================================


class TestPascalToKebab:
    """Tests for the _pascal_to_kebab helper."""

    def test_single_word_lowercase(self) -> None:
        assert _pascal_to_kebab("Greeter") == "greeter"

    def test_two_words(self) -> None:
        assert _pascal_to_kebab("CsvPipeline") == "csv-pipeline"

    def test_three_words(self) -> None:
        assert _pascal_to_kebab("MyAwesomeApp") == "my-awesome-app"

    def test_consecutive_uppercase(self) -> None:
        assert _pascal_to_kebab("HTTPHandler") == "http-handler"

    def test_single_uppercase_prefix(self) -> None:
        assert _pascal_to_kebab("S3Loader") == "s3-loader"

    def test_already_lowercase(self) -> None:
        assert _pascal_to_kebab("myapp") == "myapp"


# =============================================================================
# AppError
# =============================================================================


class TestAppError:
    """Tests for AppError."""

    def test_str_includes_error_code(self) -> None:
        """AppError.__str__ includes the error code."""
        err = AppError("Something went wrong")
        result = str(err)
        assert APP_ERROR.code in result
        assert "Something went wrong" in result

    def test_str_includes_app_name_when_provided(self) -> None:
        """AppError.__str__ includes app_name when provided."""
        err = AppError("Error", app_name="my-app")
        result = str(err)
        assert "my-app" in result

    def test_str_includes_run_id_when_provided(self) -> None:
        """AppError.__str__ includes run_id when provided."""
        err = AppError("Error", run_id="run-123")
        result = str(err)
        assert "run-123" in result

    def test_error_code_is_app_error(self) -> None:
        """AppError.error_code defaults to APP_ERROR."""
        err = AppError("Error")
        assert err.error_code == APP_ERROR

    def test_custom_error_code(self) -> None:
        """Custom error_code can be provided."""
        err = AppError("Error", error_code=APP_NON_RETRYABLE)
        assert err.error_code == APP_NON_RETRYABLE


class TestNonRetryableError:
    """Tests for NonRetryableError."""

    def test_has_non_retryable_error_code(self) -> None:
        """NonRetryableError has APP_NON_RETRYABLE error code."""
        err = NonRetryableError("Not retryable")
        assert err.error_code == APP_NON_RETRYABLE

    def test_is_subclass_of_app_error(self) -> None:
        """NonRetryableError is a subclass of AppError."""
        err = NonRetryableError("Not retryable")
        assert isinstance(err, AppError)

    def test_str_includes_non_retryable_code(self) -> None:
        """NonRetryableError.__str__ includes APP_NON_RETRYABLE code."""
        err = NonRetryableError("Not retryable")
        result = str(err)
        assert APP_NON_RETRYABLE.code in result


# =============================================================================
# App registration via __init_subclass__
# =============================================================================


class TestAppRegistration:
    """Tests for App auto-registration via __init_subclass__."""

    def setup_method(self) -> None:
        """Reset registries before each test."""
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        """Reset registries after each test."""
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_concrete_app_auto_registers(self) -> None:
        """A concrete App subclass is automatically registered."""

        class MyGreeter(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        registry = AppRegistry.get_instance()
        # Should be registered with kebab-case name
        metadata = registry.get("my-greeter")
        assert metadata is not None
        assert metadata.name == "my-greeter"

    def test_app_name_derived_from_class_name(self) -> None:
        """App name is derived from class name (PascalCase -> kebab-case)."""

        class CsvPipelineApp(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        registry = AppRegistry.get_instance()
        metadata = registry.get("csv-pipeline-app")
        assert metadata.name == "csv-pipeline-app"

    def test_app_name_class_var_overrides_derived(self) -> None:
        """App.name class var overrides the derived name."""

        class SomeInternalName(App):
            name = "my-custom-name"

            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        registry = AppRegistry.get_instance()
        metadata = registry.get("my-custom-name")
        assert metadata.name == "my-custom-name"

    def test_app_version_class_var_overrides_default(self) -> None:
        """App.version class var overrides the default version."""

        class VersionedApp(App):
            version = "2.5.0"

            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        registry = AppRegistry.get_instance()
        metadata = registry.get("versioned-app")
        assert metadata.version == "2.5.0"

    def test_abstract_app_not_registered(self) -> None:
        """An App subclass without run() implementation is NOT registered."""
        import inspect

        class AbstractSubApp(App):
            # No run() implementation — still abstract
            pass

        assert inspect.isabstract(AbstractSubApp)
        registry = AppRegistry.get_instance()
        # Should not be registered
        with pytest.raises(Exception):
            registry.get("abstract-sub-app")

    def test_app_without_type_hints_not_registered(self) -> None:
        """An App subclass with run() but no type hints is NOT registered."""

        class NoHintsApp(App):
            async def run(self, input):  # type: ignore[override]
                return SimpleOutput()

        registry = AppRegistry.get_instance()
        with pytest.raises(Exception):
            registry.get("no-hints-app")

    def test_registered_app_has_correct_input_type(self) -> None:
        """After registration, _input_type is set correctly."""

        class TypedApp(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert TypedApp._input_type is SimpleInput

    def test_registered_app_has_correct_output_type(self) -> None:
        """After registration, _output_type is set correctly."""

        class TypedApp2(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert TypedApp2._output_type is SimpleOutput

    def test_registered_app_has_app_name_set(self) -> None:
        """After registration, _app_name is set."""

        class NamedApp(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert NamedApp._app_name == "named-app"

    def test_registered_app_has_app_version_set(self) -> None:
        """After registration, _app_version is set."""

        class VersionApp(App):
            version = "3.0.0"

            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert VersionApp._app_version == "3.0.0"

    def test_app_with_wrong_input_type_not_registered(self) -> None:
        """App with non-Input run() parameter is NOT registered."""

        class BadInputApp(App):
            async def run(self, input: str) -> SimpleOutput:  # type: ignore[override]
                return SimpleOutput()

        registry = AppRegistry.get_instance()
        with pytest.raises(Exception):
            registry.get("bad-input-app")

    def test_app_with_wrong_output_type_not_registered(self) -> None:
        """App with non-Output run() return type is NOT registered."""

        class BadOutputApp(App):
            async def run(self, input: SimpleInput) -> str:  # type: ignore[override]
                return "bad"

        registry = AppRegistry.get_instance()
        with pytest.raises(Exception):
            registry.get("bad-output-app")

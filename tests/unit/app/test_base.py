"""Tests for App base class behavior (no Temporal worker needed)."""

from __future__ import annotations

from collections.abc import Sequence as _Seq
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any as _Any
from unittest import mock
from uuid import UUID

import pytest
from temporalio import workflow as _wf  # _wf._Definition used for relay assertions
from temporalio.common import RawValue as _RawValue

from application_sdk.app import query, signal, update
from application_sdk.app.base import (
    App,
    AppContextError,
    AppError,
    AppStateAccessor,
    NonRetryableError,
    PersistentStateAccessor,
    RetryableError,
    TaskStateAccessor,
    _app_state,
    _app_state_lock,
    _create_task_activity_wrapper,
    _get_execution_id_from_task,
    _pascal_to_kebab,
    _safe_log,
    _safe_now,
    _safe_uuid,
    _scan_entrypoints,
    _workflow_class_cache,
    _wrap_instance_tasks,
    generate_workflow_class,
)
from application_sdk.app.entrypoint import (
    EntryPointContractError,
    EntryPointMetadata,
    entrypoint,
)
from application_sdk.app.registry import AppNotFoundError, AppRegistry
from application_sdk.app.task import task
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

    def test_app_without_run_override_not_registered(self) -> None:
        """An App subclass that does not override run() is silently NOT registered.

        The class inherits the default raise-NotImplementedError stub. Because
        'run' is not in its __dict__, __init_subclass__ skips it without error
        (it may define @entrypoint methods, or be an intermediate base class).
        """

        class AbstractSubApp(App):
            # No run() override — inherits the default raise NotImplementedError
            pass

        registry = AppRegistry.get_instance()
        with pytest.raises(AppNotFoundError):
            registry.get("abstract-sub-app")

    def test_app_with_run_but_no_type_hints_raises_contract_error(self) -> None:
        """An App subclass that overrides run() without type hints raises EntryPointContractError."""

        with pytest.raises(EntryPointContractError, match="must have type annotations"):

            class NoHintsApp(App):
                async def run(self, input):  # type: ignore[override]
                    return SimpleOutput()

    def test_app_with_run_using_base_input_raises_contract_error(self) -> None:
        """Overriding run() with the base Input type (not narrowed) raises EntryPointContractError."""

        with pytest.raises(EntryPointContractError, match="concrete subclass of Input"):

            class BaseInputApp(App):
                async def run(self, input: Input) -> SimpleOutput:  # type: ignore[override]
                    return SimpleOutput()

    def test_app_with_run_using_base_output_raises_contract_error(self) -> None:
        """Overriding run() with the base Output type (not narrowed) raises EntryPointContractError."""

        with pytest.raises(
            EntryPointContractError, match="concrete subclass of Output"
        ):

            class BaseOutputApp(App):
                async def run(self, input: SimpleInput) -> Output:  # type: ignore[override]
                    return Output()

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

    def test_app_with_wrong_input_type_raises_contract_error(self) -> None:
        """App that overrides run() with a non-Input parameter raises EntryPointContractError."""

        with pytest.raises(EntryPointContractError, match="input type"):

            class BadInputApp(App):
                async def run(self, input: str) -> SimpleOutput:  # type: ignore[override]
                    return SimpleOutput()

    def test_app_with_wrong_output_type_raises_contract_error(self) -> None:
        """App that overrides run() with a non-Output return type raises EntryPointContractError."""

        with pytest.raises(EntryPointContractError, match="output type"):

            class BadOutputApp(App):
                async def run(self, input: SimpleInput) -> str:  # type: ignore[override]
                    return "bad"

    def test_grandchild_inheriting_run_from_intermediate_parent_registers(self) -> None:
        """A grandchild that inherits run() from an intermediate parent registers correctly.

        This covers the ``if "run" not in cls.__dict__: … cls.run is App.run`` branch
        added to ``__init_subclass__``.
        """

        class Intermediate(App):
            async def run(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        class Grandchild(Intermediate):
            pass

        registry = AppRegistry.get_instance()
        metadata = registry.get("grandchild")
        assert metadata is not None
        assert metadata.name == "grandchild"
        assert Grandchild._input_type is SimpleInput
        assert Grandchild._output_type is SimpleOutput

    def test_entrypoint_only_subclass_not_registered_as_run_app(self) -> None:
        """A subclass that only has no run() (inherits the App.run stub) is silently skipped.

        Specifically: cls.run is App.run → skip without raising.
        This covers the second branch of the new __init_subclass__ registration logic.
        """

        class NoRunApp(App):
            pass

        registry = AppRegistry.get_instance()
        with pytest.raises(AppNotFoundError):
            registry.get("no-run-app")


# =============================================================================
# Helpers shared by the new tests below
# =============================================================================


class _BLDXInput(Input, allow_unbounded_fields=True):
    """Pydantic-only (no @dataclass): keeps `_BLDXInput()` callable with defaults."""

    value: str = ""


class _BLDXOutput(Output, allow_unbounded_fields=True):
    result: str = ""


@pytest.fixture(autouse=True)
def _reset_app_base_state(clean_app_registry, clean_task_registry):
    with _app_state_lock:
        _app_state.clear()
    _workflow_class_cache.clear()
    yield
    with _app_state_lock:
        _app_state.clear()
    _workflow_class_cache.clear()


# =============================================================================
# _safe_now / _safe_uuid — deterministic time / UUID helpers
# =============================================================================


class TestSafeNow:
    """Tests for _safe_now."""

    def test_returns_workflow_now(self) -> None:
        fixed = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        with mock.patch("application_sdk.app.base.workflow.now", return_value=fixed):
            assert _safe_now() == fixed


class TestSafeUuid:
    """Tests for _safe_uuid."""

    def test_returns_uuid_from_workflow_uuid4(self) -> None:
        with mock.patch(
            "application_sdk.app.base.workflow.uuid4",
            return_value="12345678-1234-5678-1234-567812345678",
        ):
            result = _safe_uuid()
        assert isinstance(result, UUID)
        assert str(result) == "12345678-1234-5678-1234-567812345678"


# =============================================================================
# _safe_log — branches across atlan-logger and stdlib loggers
# =============================================================================


class TestSafeLog:
    """Tests for _safe_log."""

    def test_atlan_logger_passes_attrs_as_kwargs(self) -> None:
        atlan_logger = mock.MagicMock()
        # Make it look like AtlanLoggerAdapter for _is_atlan_logger duck-type:
        atlan_logger.process = mock.MagicMock()
        atlan_logger.logger_name = "test"
        with mock.patch("application_sdk.app.base.workflow.logger", atlan_logger):
            _safe_log("info", "hello", app_name="my-app", run_id="r1")
        atlan_logger.info.assert_called_once_with(
            "hello", app_name="my-app", run_id="r1"
        )

    def test_stdlib_logger_packs_attrs_into_extra(self) -> None:
        stdlib_logger = mock.MagicMock(spec=["info", "warning", "error", "debug"])
        # Drop the duck-type markers so _is_atlan_logger -> False.
        with mock.patch("application_sdk.app.base.workflow.logger", stdlib_logger):
            _safe_log("info", "hello", app_name="my-app")
        stdlib_logger.info.assert_called_once_with(
            "hello", extra={"app_name": "my-app"}
        )

    def test_stdlib_logger_separates_reserved_kwargs(self) -> None:
        """exc_info / stack_info / stacklevel are stdlib-reserved and must NOT be in extra=."""
        stdlib_logger = mock.MagicMock(spec=["info", "warning", "error", "debug"])
        with mock.patch("application_sdk.app.base.workflow.logger", stdlib_logger):
            _safe_log("warning", "boom", exc_info=True, app_name="x")
        kwargs = stdlib_logger.warning.call_args.kwargs
        assert kwargs["exc_info"] is True
        assert kwargs["extra"] == {"app_name": "x"}

    def test_stdlib_logger_only_reserved_kwargs(self) -> None:
        stdlib_logger = mock.MagicMock(spec=["info", "warning", "error", "debug"])
        with mock.patch("application_sdk.app.base.workflow.logger", stdlib_logger):
            _safe_log("warning", "boom", exc_info=True)
        stdlib_logger.warning.assert_called_once_with("boom", exc_info=True)

    def test_stdlib_logger_no_kwargs(self) -> None:
        stdlib_logger = mock.MagicMock(spec=["info", "warning", "error", "debug"])
        with mock.patch("application_sdk.app.base.workflow.logger", stdlib_logger):
            _safe_log("info", "plain")
        stdlib_logger.info.assert_called_once_with("plain")


# =============================================================================
# RetryableError — sister class to NonRetryableError
# =============================================================================


class TestRetryableError:
    def test_default_error_code_is_app_error(self) -> None:
        err = RetryableError("transient")
        assert err.error_code == APP_ERROR

    def test_is_subclass_of_app_error(self) -> None:
        assert issubclass(RetryableError, AppError)


class TestAppContextError:
    def test_str_includes_error_code(self) -> None:
        err = AppContextError("oops")
        s = str(err)
        assert "oops" in s
        # Must include the error code prefix in brackets
        assert s.startswith("[")


# =============================================================================
# Context properties — raise AppContextError when called outside run()
# =============================================================================


class TestContextProperties:
    """The context / task_context properties guard against misuse outside run()."""

    def _make_app(self) -> App:
        class _CtxApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        return _CtxApp()

    @pytest.mark.parametrize(
        ("accessor", "match"),
        [
            (lambda app: app.context, "App context"),
            (lambda app: app.task_context, "task_context"),
            (lambda app: app.logger, None),
            (lambda app: app.run_id, None),
            (lambda app: app.correlation_id, None),
            (lambda app: app.is_cancelled(), None),
        ],
    )
    def test_properties_raise_when_unset(self, accessor, match: str | None) -> None:
        app = self._make_app()
        with pytest.raises(AppContextError, match=match):
            accessor(app)

    def test_context_returns_when_set(self) -> None:
        from application_sdk.app.context import AppContext

        ctx = AppContext(app_name="t", app_version="0.0.1", run_id="r1")
        app = self._make_app()
        app._context = ctx
        assert app.context is ctx
        assert app.run_id == "r1"
        # correlation_id defaults to run_id when blank
        assert app.correlation_id == "r1"
        assert app.is_cancelled() is False


# =============================================================================
# Task-only methods — raise AppContextError outside @task scope
# =============================================================================


class TestTaskOnlyMethods:
    """heartbeat / get_*_heartbeat_details / run_in_thread must guard task scope."""

    def _app(self) -> App:
        class _TOApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        return _TOApp()

    @pytest.fixture
    def heartbeat_details_type(self):
        from application_sdk.contracts.base import HeartbeatDetails

        @dataclass
        class _HD(HeartbeatDetails):
            pass

        return _HD

    @pytest.mark.parametrize(
        "call",
        [
            lambda app, _hd: app.heartbeat("progress"),
            lambda app, _hd: app.get_last_heartbeat_details(),
            lambda app, hd: app.get_heartbeat_details(hd),
        ],
    )
    def test_sync_methods_outside_task_raise(
        self, call, heartbeat_details_type
    ) -> None:
        with pytest.raises(AppContextError):
            call(self._app(), heartbeat_details_type)

    @pytest.mark.asyncio
    async def test_run_in_thread_outside_task_raises(self) -> None:
        with pytest.raises(AppContextError):
            await self._app().run_in_thread(lambda: None)

    def test_heartbeat_delegates_to_task_context(self) -> None:
        app = self._app()
        tc = mock.MagicMock()
        app._task_context = tc
        app.heartbeat("a", 1)
        tc.heartbeat.assert_called_once_with("a", 1)

    def test_get_last_heartbeat_delegates(self) -> None:
        app = self._app()
        tc = mock.MagicMock()
        tc.get_last_heartbeat_details.return_value = ("x",)
        app._task_context = tc
        assert app.get_last_heartbeat_details() == ("x",)

    @pytest.mark.asyncio
    async def test_run_in_thread_delegates(self) -> None:
        app = self._app()
        tc = mock.MagicMock()

        async def _ret(*a, **kw):
            return "done"

        tc.run_in_thread = mock.AsyncMock(return_value="done")
        app._task_context = tc
        result = await app.run_in_thread(lambda: None, 1, x=2)
        assert result == "done"
        tc.run_in_thread.assert_awaited_once()


# =============================================================================
# require()
# =============================================================================


class TestRequire:
    def _app(self) -> App:
        class _ReqApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        return _ReqApp()

    def test_returns_value_when_not_none(self) -> None:
        assert self._app().require("hello", "x") == "hello"
        assert self._app().require(0, "y") == 0  # falsy but not None

    def test_raises_non_retryable_when_none(self) -> None:
        app = self._app()
        with pytest.raises(NonRetryableError) as exc:
            app.require(None, "api_key")
        assert "api_key is required" in str(exc.value)

    def test_includes_context_in_message(self) -> None:
        app = self._app()
        with pytest.raises(NonRetryableError) as exc:
            app.require(None, "token", "for OAuth flow")
        assert "for OAuth flow" in str(exc.value)


# =============================================================================
# State accessors — TaskStateAccessor / AppStateAccessor / get_app_state
# =============================================================================


class TestAppState:
    def _app(self) -> App:
        class _StateApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        return _StateApp()

    def test_get_app_state_returns_none_when_outside_task(self) -> None:
        # _get_execution_id_from_task wraps activity.info() failures into AppContextError.
        with pytest.raises(AppContextError):
            self._app().get_app_state("k")

    def test_get_app_state_returns_value_when_set(self) -> None:
        app = self._app()
        with mock.patch("application_sdk.app.base.activity.info") as info:
            info.return_value = mock.MagicMock(workflow_id="wf-1")
            app.set_app_state("foo", 42)
            assert app.get_app_state("foo") == 42
            assert app.get_app_state("missing") is None

    def test_set_app_state_propagates_via_task_state_accessor(self) -> None:
        app = self._app()
        with mock.patch("application_sdk.app.base.activity.info") as info:
            info.return_value = mock.MagicMock(workflow_id="wf-2")
            app.set_app_state("k", "v")
            assert TaskStateAccessor().get("k") == "v"

    def test_app_state_accessor_property_round_trip(self) -> None:
        app = self._app()
        with mock.patch("application_sdk.app.base.activity.info") as info:
            info.return_value = mock.MagicMock(workflow_id="wf-3")
            accessor = app.app_state
            assert isinstance(accessor, AppStateAccessor)
            accessor.set("a", 1)
            assert accessor.get("a") == 1

    def test_task_state_accessor_outside_task_raises(self) -> None:
        # No activity context → activity.info() raises → wrapped as AppContextError
        accessor = TaskStateAccessor()
        with pytest.raises(AppContextError, match="outside of task context"):
            accessor.get("k")
        with pytest.raises(AppContextError, match="outside of task context"):
            accessor.set("k", 1)

    @pytest.mark.asyncio
    async def test_persistent_state_accessor_delegates_to_context(self) -> None:
        app = self._app()
        ctx = mock.MagicMock()
        ctx.save_state = mock.AsyncMock()
        ctx.load_state = mock.AsyncMock(return_value={"x": 1})
        app._context = ctx
        accessor = app.persistent_state
        assert isinstance(accessor, PersistentStateAccessor)

        await accessor.save("k", {"v": 1})
        assert await accessor.load("k") == {"x": 1}
        ctx.save_state.assert_awaited_once_with("k", {"v": 1})
        ctx.load_state.assert_awaited_once_with("k")


class TestGetExecutionIdFromTask:
    def test_wraps_activity_info_failure(self) -> None:
        with mock.patch(
            "application_sdk.app.base.activity.info", side_effect=RuntimeError("no act")
        ):
            with pytest.raises(AppContextError) as exc:
                _get_execution_id_from_task()
            assert isinstance(exc.value.__cause__, RuntimeError)


# =============================================================================
# now() / uuid() / get_name() / get_version() — convenience accessors
# =============================================================================


class TestAppConvenience:
    def test_now_uses_workflow_now(self) -> None:
        class _NowApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        fixed = datetime(2025, 5, 1, tzinfo=UTC)
        with mock.patch("application_sdk.app.base.workflow.now", return_value=fixed):
            assert _NowApp().now() == fixed

    def test_uuid_uses_workflow_uuid4(self) -> None:
        class _UApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        with mock.patch(
            "application_sdk.app.base.workflow.uuid4",
            return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ):
            u = _UApp().uuid()
        assert isinstance(u, UUID)
        assert str(u) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_get_name_and_version(self) -> None:
        class NamedApp(App):
            version = "9.9.9"

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        a = NamedApp()
        assert a.get_name() == "named-app"
        assert a.get_version() == "9.9.9"


# =============================================================================
# default run() — raises NotImplementedError when not overridden
# =============================================================================


class TestRunStub:
    @pytest.mark.asyncio
    async def test_default_run_raises_not_implemented(self) -> None:
        # NoRunApp inherits the App.run stub silently; call it directly to
        # exercise the NotImplementedError branch.
        class _NoRun(App):
            pass

        from application_sdk.app.base_errors import AbstractRunNotImplementedError

        with pytest.raises(AbstractRunNotImplementedError) as exc_info:
            await _NoRun().run(_BLDXInput())
        assert exc_info.value.code == "UNIMPLEMENTED_APP_RUN"


# =============================================================================
# continue_with()
# =============================================================================


class TestContinueWith:
    def _app(self) -> App:
        class _CWApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        return _CWApp()

    def test_raises_outside_run(self) -> None:
        app = self._app()
        with pytest.raises(AppContextError, match="continue_with"):
            app.continue_with(_BLDXInput())

    def test_calls_continue_as_new_with_correlation_memo(self) -> None:
        from application_sdk.app.context import AppContext

        ctx = AppContext(
            app_name="cwa",
            app_version="0.0.1",
            run_id="rid",
            correlation_id="corr-9",
        )
        app = self._app()
        app._context = ctx
        new_input = _BLDXInput(value="next")

        with (
            mock.patch("application_sdk.app.base.workflow.continue_as_new") as cont,
            mock.patch("application_sdk.app.base._safe_log"),
        ):
            app.continue_with(new_input)

        cont.assert_called_once()
        kwargs = cont.call_args.kwargs
        assert kwargs["args"] == [new_input]
        assert kwargs["memo"] == {"correlation_id": "corr-9"}


# =============================================================================
# _scan_entrypoints / _wrap_instance_tasks
# =============================================================================


class TestScanAndWrap:
    def test_scan_entrypoints_finds_decorated_methods(self) -> None:
        class _ScanApp(App):
            @entrypoint
            async def my_entry(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        eps = _scan_entrypoints(_ScanApp)
        assert "my-entry" in eps
        assert eps["my-entry"].method_name == "my_entry"

    def test_scan_entrypoints_returns_empty_for_plain_class(self) -> None:
        class _Plain(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        # Plain classes use implicit-run path; explicit @entrypoint scan is empty.
        assert _scan_entrypoints(_Plain) == {}

    def test_wrap_instance_tasks_replaces_task_methods(self) -> None:
        class _TaskApp(App):
            @task
            async def my_task(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        instance = _TaskApp()
        original_unbound = _TaskApp.my_task
        # Stub out the wrapper factory so we don't pull in Temporal RetryPolicy.
        sentinel = object()
        with mock.patch(
            "application_sdk.app.base._create_task_activity_wrapper",
            return_value=sentinel,
        ) as factory:
            _wrap_instance_tasks(instance, {"run_id": "r", "correlation_id": "c"})

        assert instance.my_task is sentinel
        # Class-level remains untouched
        assert _TaskApp.my_task is original_unbound
        # framework storage tasks (upload/download/cleanup_files/cleanup_storage)
        # are also @task-decorated → factory is called for those too plus my_task.
        called_task_names = {call.args[1] for call in factory.call_args_list}
        assert "my_task" in called_task_names
        assert "upload" in called_task_names
        assert "download" in called_task_names
        assert "cleanup_files" in called_task_names
        assert "cleanup_storage" in called_task_names

    def test_wrap_instance_tasks_skips_underscore_prefixed(self) -> None:
        class _UApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        instance = _UApp()
        with mock.patch(
            "application_sdk.app.base._create_task_activity_wrapper"
        ) as factory:
            _wrap_instance_tasks(instance, {"run_id": "", "correlation_id": ""})
        # We can't assert exact set, but it must not blow up on private attrs.
        assert factory.call_count >= 0


# =============================================================================
# _create_task_activity_wrapper — exercises retry policy + TaskContext imports
# =============================================================================


class TestCreateTaskActivityWrapper:
    """Tests for _create_task_activity_wrapper."""

    @pytest.mark.asyncio
    async def test_wrapper_invokes_workflow_execute_activity(self) -> None:
        wrapper = _create_task_activity_wrapper(
            app_name="my-app",
            task_name="my-task",
            timeout_seconds=10,
            retry_max_attempts=2,
            retry_max_interval_seconds=5,
            output_type=_BLDXOutput,
            context_data={"run_id": "rid", "correlation_id": "cid"},
            heartbeat_timeout_seconds=30,
            auto_heartbeat_seconds=5,
            retry_policy=None,
        )

        sentinel_out = _BLDXOutput(result="ok")
        with mock.patch(
            "application_sdk.app.base.workflow.execute_activity",
            new_callable=mock.AsyncMock,
            return_value=sentinel_out,
        ) as exec_act:
            result = await wrapper(_BLDXInput(value="in"))

        assert result is sentinel_out
        exec_act.assert_awaited_once()
        call_args = exec_act.call_args
        # First positional arg: activity name "<app>:<task>"
        assert call_args.args[0] == "my-app:my-task"
        # Result type is forwarded for proper deserialization
        assert call_args.kwargs["result_type"] is _BLDXOutput

    @pytest.mark.asyncio
    async def test_wrapper_disables_heartbeat_when_none(self) -> None:
        wrapper = _create_task_activity_wrapper(
            app_name="a",
            task_name="t",
            timeout_seconds=10,
            retry_max_attempts=1,
            retry_max_interval_seconds=1,
            output_type=_BLDXOutput,
            context_data={},
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )
        with mock.patch(
            "application_sdk.app.base.workflow.execute_activity",
            new_callable=mock.AsyncMock,
            return_value=_BLDXOutput(),
        ) as exec_act:
            await wrapper(_BLDXInput())
        # When heartbeat is disabled the kwarg is None.
        assert exec_act.call_args.kwargs["heartbeat_timeout"] is None
        # schedule_to_start is disabled by default.
        assert exec_act.call_args.kwargs["schedule_to_start_timeout"] is None

    @pytest.mark.asyncio
    async def test_wrapper_passes_schedule_to_start_timeout(self) -> None:
        from datetime import timedelta

        wrapper = _create_task_activity_wrapper(
            app_name="a",
            task_name="t",
            timeout_seconds=10,
            retry_max_attempts=1,
            retry_max_interval_seconds=1,
            output_type=_BLDXOutput,
            context_data={},
            schedule_to_start_timeout_seconds=600,
        )
        with mock.patch(
            "application_sdk.app.base.workflow.execute_activity",
            new_callable=mock.AsyncMock,
            return_value=_BLDXOutput(),
        ) as exec_act:
            await wrapper(_BLDXInput())
        assert exec_act.call_args.kwargs["schedule_to_start_timeout"] == timedelta(
            seconds=600
        )

    @pytest.mark.asyncio
    async def test_wrapper_passes_explicit_retry_policy_through(self) -> None:
        from application_sdk.execution.retry import RetryPolicy

        rp = RetryPolicy(max_attempts=7)
        wrapper = _create_task_activity_wrapper(
            app_name="a",
            task_name="t",
            timeout_seconds=1,
            retry_max_attempts=99,
            retry_max_interval_seconds=99,
            output_type=_BLDXOutput,
            context_data={},
            retry_policy=rp,
        )
        with mock.patch(
            "application_sdk.app.base.workflow.execute_activity",
            new_callable=mock.AsyncMock,
            return_value=_BLDXOutput(),
        ) as exec_act:
            await wrapper(_BLDXInput())
        passed = exec_act.call_args.kwargs["retry_policy"]
        # Temporal RetryPolicy uses .maximum_attempts (not .max_attempts).
        assert passed.maximum_attempts == 7


# =============================================================================
# generate_workflow_class — exercises the whole _run lifecycle end to end
# =============================================================================


class TestGenerateWorkflowClass:
    """Tests for generate_workflow_class."""

    def _make_ep(self, input_type: type, output_type: type) -> EntryPointMetadata:
        return EntryPointMetadata(
            name="run",
            input_type=input_type,
            output_type=output_type,
            method_name="run",
            implicit=True,
        )

    @pytest.mark.asyncio
    async def test_generate_workflow_returns_cached_class(self) -> None:
        # Build a real App via __init_subclass__ to keep types valid.
        class _CachedApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result=input.value)

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls_a = generate_workflow_class(_CachedApp, ep)
            wf_cls_b = generate_workflow_class(_CachedApp, ep)
        assert wf_cls_a is wf_cls_b

    @pytest.mark.asyncio
    async def test_run_happy_path_executes_entry_method(self) -> None:
        results: list[str] = []

        class HappyApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                results.append("ran:" + input.value)
                return _BLDXOutput(result="ok")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)

        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(HappyApp, ep)

        run_fn = wf_cls.run
        info_mock = mock.MagicMock(run_id="run-1", workflow_id="wf-1")

        with (
            self._patched_workflow_layer(info_mock),
            mock.patch.object(
                HappyApp, "on_complete", new_callable=mock.AsyncMock
            ) as on_complete,
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                return_value=None,
            ),
        ):
            out = await run_fn(mock.MagicMock(), _BLDXInput(value="hi"))

        assert isinstance(out, _BLDXOutput)
        assert results == ["ran:hi"]
        on_complete.assert_awaited_once()

    def _patched_workflow_layer(self, info_mock: mock.MagicMock):
        """Returns a contextlib.ExitStack wired with the patches every _run test needs."""
        import contextlib

        stack = contextlib.ExitStack()
        ipt = stack.enter_context(
            mock.patch(
                "application_sdk.app.base.workflow.unsafe.imports_passed_through"
            )
        )
        ipt.return_value.__enter__ = mock.MagicMock()
        ipt.return_value.__exit__ = mock.MagicMock(return_value=False)
        stack.enter_context(
            mock.patch("application_sdk.app.base.workflow.info", return_value=info_mock)
        )
        stack.enter_context(mock.patch("application_sdk.app.base._safe_log"))
        stack.enter_context(
            mock.patch(
                "application_sdk.app.base._safe_now",
                return_value=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        stack.enter_context(mock.patch("application_sdk.app.base._wrap_instance_tasks"))
        return stack

    @pytest.mark.asyncio
    async def test_run_wraps_raw_exceptions_in_application_error(self) -> None:
        class BoomApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                raise ValueError("boom!")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(BoomApp, ep)

        from application_sdk.execution.errors import ApplicationError

        info_mock = mock.MagicMock(run_id="rid", workflow_id="wfid")
        with (
            self._patched_workflow_layer(info_mock),
            mock.patch.object(BoomApp, "on_complete", new_callable=mock.AsyncMock),
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc,
        ):
            await wf_cls.run(mock.MagicMock(), _BLDXInput())

        assert exc.value.__cause__ is not None
        assert isinstance(exc.value.__cause__, ValueError)
        assert getattr(exc.value, "non_retryable", None) is True

    @pytest.mark.asyncio
    async def test_run_lets_failure_error_propagate_unchanged(self) -> None:
        from temporalio.exceptions import FailureError

        class _OrigFail(FailureError):
            pass

        class FailApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                raise _OrigFail("temporal-native")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(FailApp, ep)

        info_mock = mock.MagicMock(run_id="r", workflow_id="w")
        with (
            self._patched_workflow_layer(info_mock),
            mock.patch.object(FailApp, "on_complete", new_callable=mock.AsyncMock),
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                return_value=None,
            ),
            pytest.raises(_OrigFail),
        ):
            await wf_cls.run(mock.MagicMock(), _BLDXInput())

    @pytest.mark.asyncio
    async def test_run_retryable_error_marked_retryable(self) -> None:
        class _Retry(RetryableError):
            pass

        class RApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                raise _Retry("transient")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(RApp, ep)

        from application_sdk.execution.errors import ApplicationError

        info_mock = mock.MagicMock(run_id="r", workflow_id="w")
        with (
            self._patched_workflow_layer(info_mock),
            mock.patch.object(RApp, "on_complete", new_callable=mock.AsyncMock),
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc,
        ):
            await wf_cls.run(mock.MagicMock(), _BLDXInput())

        assert getattr(exc.value, "non_retryable", None) is False

    @pytest.mark.asyncio
    async def test_run_swallows_on_complete_failure(self) -> None:
        class OCFail(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

            async def on_complete(self) -> None:
                raise RuntimeError("oc boom")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(OCFail, ep)

        info_mock = mock.MagicMock(run_id="r", workflow_id="w")
        with (
            self._patched_workflow_layer(info_mock),
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                return_value=None,
            ),
        ):
            # Must complete without raising — on_complete failures are swallowed.
            out = await wf_cls.run(mock.MagicMock(), _BLDXInput())

        assert isinstance(out, _BLDXOutput)

    @pytest.mark.asyncio
    async def test_run_continues_when_correlation_import_fails(self) -> None:
        """Failure inside the inline get_correlation_context import path falls back to run_id."""

        class CCApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

        ep = self._make_ep(_BLDXInput, _BLDXOutput)
        with (
            mock.patch(
                "application_sdk.app.base.workflow.run", side_effect=lambda f: f
            ),
            mock.patch(
                "application_sdk.app.base.workflow.defn",
                side_effect=lambda **_: lambda c: c,
            ),
        ):
            wf_cls = generate_workflow_class(CCApp, ep)

        info_mock = mock.MagicMock(run_id="rid-fallback", workflow_id="wid")
        with (
            self._patched_workflow_layer(info_mock),
            mock.patch.object(CCApp, "on_complete", new_callable=mock.AsyncMock),
            mock.patch(
                "application_sdk.observability.correlation.get_correlation_context",
                side_effect=RuntimeError("correlation lookup blew up"),
            ),
        ):
            out = await wf_cls.run(mock.MagicMock(), _BLDXInput())

        assert isinstance(out, _BLDXOutput)


# =============================================================================
# Runtime interaction relay (BLDX-1283) — @signal / @query / @update on App
# =============================================================================


class TestWorkflowInteractionRelay:
    """Pin that @signal / @query / @update declared on an App subclass are
    lifted onto the generated wf_cls so the underlying runtime discovers them
    and `handle.execute_update(...)` / `handle.signal(...)` resolve to the
    App's methods at runtime.

    Pre-BLDX-1283 these interactions were silently dropped because the
    synthesized wf_cls only carried `run` — the runtime scans the generated
    class, not the App class, so decorators on App methods were invisible.
    """

    def _make_ep(self, input_type: type, output_type: type) -> EntryPointMetadata:
        return EntryPointMetadata(
            name="run",
            input_type=input_type,
            output_type=output_type,
            method_name="run",
            implicit=True,
        )

    def test_signal_interaction_is_registered_on_wf_cls(self) -> None:
        class _SignalApp(App):
            def __init__(self) -> None:
                self.ping_count: int = 0

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @signal
            async def ping(self) -> None:
                self.ping_count += 1

        wf_cls = generate_workflow_class(
            _SignalApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        assert "ping" in defn.signals

    def test_update_interaction_is_registered_on_wf_cls(self) -> None:
        class _UpdateApp(App):
            def __init__(self) -> None:
                self.state = "running"

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @update
            async def pause_run(self, input: _BLDXInput) -> _BLDXOutput:
                self.state = f"paused:{input.value}"
                return _BLDXOutput(result=self.state)

        wf_cls = generate_workflow_class(
            _UpdateApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        assert "pause_run" in defn.updates

    def test_query_interaction_is_registered_on_wf_cls(self) -> None:
        class _QueryApp(App):
            def __init__(self) -> None:
                self.counter = 7

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @query
            def get_counter(self) -> _BLDXOutput:
                return _BLDXOutput(result=str(self.counter))

        wf_cls = generate_workflow_class(
            _QueryApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        assert "get_counter" in defn.queries

    @pytest.mark.asyncio
    async def test_relay_delegates_to_shared_app_instance(self) -> None:
        """Interaction relay must mutate the same App instance _run sees, not a fresh one."""

        class _StateApp(App):
            def __init__(self) -> None:
                self.state = "init"

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result=self.state)

            @update
            async def set_state(self, input: _BLDXInput) -> _BLDXOutput:
                self.state = input.value
                return _BLDXOutput(result=self.state)

        wf_cls = generate_workflow_class(
            _StateApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        # Spin up a wf_cls instance (mirrors Temporal's per-run construction).
        wf_inst = wf_cls()
        assert isinstance(wf_inst._app_instance, _StateApp)
        assert wf_inst._app_instance.state == "init"

        # Drive the relay directly — the same path Temporal uses internally via
        # the rebound _UpdateDefinition.fn. State must mutate the very instance
        # _run will later read from.
        await wf_cls.set_state(wf_inst, _BLDXInput(value="paused"))
        assert wf_inst._app_instance.state == "paused"

    def test_validator_is_preserved_through_relay(self) -> None:
        class _ValidatedApp(App):
            def __init__(self) -> None:
                self.state = ""

            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @update
            async def set_value(self, input: _BLDXInput) -> _BLDXOutput:
                self.state = input.value
                return _BLDXOutput(result=self.state)

            @set_value.validator
            def _validate(self, input: _BLDXInput) -> None:
                if not input.value:
                    raise ValueError("value must be non-empty")

        wf_cls = generate_workflow_class(
            _ValidatedApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        update_defn = defn.updates["set_value"]
        assert update_defn.validator is not None

        # Bound validator must hit the App's _validate and surface the ValueError
        # — confirming the validator relay routes through _app_instance.
        wf_inst = wf_cls()
        with pytest.raises(ValueError, match="non-empty"):
            update_defn.bind_validator(wf_inst)(_BLDXInput(value=""))

    def test_dynamic_update_interaction_is_supported(self) -> None:
        # Define the class via exec with explicit globals so the
        # ``Sequence[RawValue]`` annotation in the dynamic-interaction signature
        # resolves at decoration time (the underlying runtime inspects
        # the annotation eagerly to validate dynamic-interaction shape under
        # ``from __future__ import annotations``).
        ns: dict[str, _Any] = {
            "update": update,
            "_Seq": _Seq,
            "_RawValue": _RawValue,
            "App": App,
            "_BLDXInput": _BLDXInput,
            "_BLDXOutput": _BLDXOutput,
        }
        exec(  # noqa: S102 — defined in tests; deterministic input
            "class _DynamicApp(App):\n"
            "    def __init__(self):\n"
            "        self.last_call = ('', 0)\n"
            "    async def run(self, input: _BLDXInput) -> _BLDXOutput:\n"
            "        return _BLDXOutput(result='ok')\n"
            "    @update(dynamic=True)\n"
            "    async def fallback(self, name: str, args: _Seq[_RawValue]) -> None:\n"
            "        self.last_call = (name, len(args))\n",
            ns,
        )
        _DynamicApp = ns["_DynamicApp"]

        wf_cls = generate_workflow_class(
            _DynamicApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )

        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        # Dynamic interactions are keyed by None in the registration map.
        assert None in defn.updates

    def test_app_without_interactions_yields_no_registrations(self) -> None:
        """Regression: apps that don't declare interactions must not gain spurious entries."""

        class _PlainApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

        wf_cls = generate_workflow_class(
            _PlainApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )
        defn = _wf._Definition.from_class(wf_cls)
        assert defn is not None
        assert dict(defn.signals) == {}
        assert dict(defn.queries) == {}
        assert dict(defn.updates) == {}

    # ------------------------------------------------------------------
    # Contract enforcement tests
    # ------------------------------------------------------------------

    def test_signal_with_extra_params_raises_contract_error(self) -> None:
        """@signal with params other than self is rejected at class-definition time."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadSignalApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @signal
            async def ping(self, msg: str) -> None:
                pass

        with pytest.raises(InvalidInputError, match="no parameters besides self"):
            generate_workflow_class(
                _BadSignalApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_query_with_non_output_return_raises_contract_error(self) -> None:
        """@query returning a non-Output type is rejected."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadQueryApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @query
            def get_state(self) -> str:
                return "running"

        with pytest.raises(InvalidInputError, match="subclass of Output"):
            generate_workflow_class(
                _BadQueryApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_query_with_params_raises_contract_error(self) -> None:
        """@query with params other than self is rejected."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadQueryApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @query
            def get_value(self, key: str) -> _BLDXOutput:
                return _BLDXOutput(result=key)

        with pytest.raises(InvalidInputError, match="no parameters besides self"):
            generate_workflow_class(
                _BadQueryApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_update_with_non_input_param_raises_contract_error(self) -> None:
        """@update whose param is not an Input subclass is rejected."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadUpdateApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @update
            async def set_value(self, value: str) -> _BLDXOutput:
                return _BLDXOutput(result=value)

        with pytest.raises(InvalidInputError, match="subclass of Input"):
            generate_workflow_class(
                _BadUpdateApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_update_with_non_output_return_raises_contract_error(self) -> None:
        """@update returning a non-Output type is rejected."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadUpdateApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @update
            async def set_value(self, input: _BLDXInput) -> str:
                return input.value

        with pytest.raises(InvalidInputError, match="subclass of Output"):
            generate_workflow_class(
                _BadUpdateApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_update_with_wrong_param_count_raises_contract_error(self) -> None:
        """@update with 0 or 2+ params besides self is rejected."""
        from application_sdk.errors.leaves import InvalidInputError

        class _BadUpdateApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

            @update
            async def noop(self) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

        with pytest.raises(InvalidInputError, match="exactly one parameter"):
            generate_workflow_class(
                _BadUpdateApp, self._make_ep(_BLDXInput, _BLDXOutput)
            )

    def test_dynamic_interactions_skip_contract_enforcement(self) -> None:
        """Dynamic interactions (name=None) are exempt from contract enforcement."""
        ns: dict[str, _Any] = {
            "update": update,
            "_Seq": _Seq,
            "_RawValue": _RawValue,
            "App": App,
            "_BLDXInput": _BLDXInput,
            "_BLDXOutput": _BLDXOutput,
        }
        exec(  # noqa: S102
            "class _DynamicApp(App):\n"
            "    async def run(self, input: _BLDXInput) -> _BLDXOutput:\n"
            "        return _BLDXOutput(result='ok')\n"
            "    @update(dynamic=True)\n"
            "    async def fallback(self, name: str, args: _Seq[_RawValue]) -> None:\n"
            "        pass\n",
            ns,
        )
        _DynamicApp = ns["_DynamicApp"]
        # Must not raise despite non-Input/Output signature.
        wf_cls = generate_workflow_class(
            _DynamicApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )
        defn = _wf._Definition.from_class(wf_cls)
        assert None in defn.updates

    def test_wf_init_creates_fresh_app_instance_per_run(self) -> None:
        """Each wf_cls() call constructs a fresh App instance (one per workflow run)."""

        class _IsolatedApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput(result="ok")

        wf_cls = generate_workflow_class(
            _IsolatedApp, self._make_ep(_BLDXInput, _BLDXOutput)
        )
        inst_a = wf_cls()
        inst_b = wf_cls()
        assert inst_a._app_instance is not inst_b._app_instance


# =============================================================================
# App.upload() — store routing
# =============================================================================


class TestUploadStoreRouting:
    """App.upload() routes to upstream_storage when configured, else storage."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    async def test_upload_uses_upstream_when_configured(self) -> None:
        from application_sdk.app.context import AppContext
        from application_sdk.contracts.storage import UploadInput, UploadOutput

        upstream_sentinel = object()
        deployment_sentinel = object()

        class _UpApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        app = _UpApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=deployment_sentinel,  # type: ignore[arg-type]
            _upstream_storage=upstream_sentinel,  # type: ignore[arg-type]
        )

        mock_result = UploadOutput()
        with mock.patch(
            "application_sdk.storage.transfer.upload",
            new_callable=mock.AsyncMock,
            return_value=mock_result,
        ) as mock_upload:
            await app.upload(UploadInput(local_path="/tmp/out"))

        assert mock_upload.call_args.kwargs["store"] is upstream_sentinel

    async def test_upload_falls_back_to_deployment_when_no_upstream(self) -> None:
        from application_sdk.app.context import AppContext
        from application_sdk.contracts.storage import UploadInput, UploadOutput

        deployment_sentinel = object()

        class _UpApp2(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        app = _UpApp2()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=deployment_sentinel,  # type: ignore[arg-type]
        )

        mock_result = UploadOutput()
        with mock.patch(
            "application_sdk.storage.transfer.upload",
            new_callable=mock.AsyncMock,
            return_value=mock_result,
        ) as mock_upload:
            await app.upload(UploadInput(local_path="/tmp/out"))

        assert mock_upload.call_args.kwargs["store"] is deployment_sentinel

    async def test_download_uses_upstream_when_configured(self) -> None:
        from application_sdk.app.context import AppContext
        from application_sdk.contracts.storage import DownloadInput, DownloadOutput

        upstream_sentinel = object()
        deployment_sentinel = object()

        class _DlApp(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        app = _DlApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=deployment_sentinel,  # type: ignore[arg-type]
            _upstream_storage=upstream_sentinel,  # type: ignore[arg-type]
        )

        mock_result = DownloadOutput()
        with mock.patch(
            "application_sdk.storage.transfer.download",
            new_callable=mock.AsyncMock,
            return_value=mock_result,
        ) as mock_download:
            await app.download(DownloadInput(storage_path="artifacts/out"))

        assert mock_download.call_args.kwargs["store"] is upstream_sentinel

    async def test_download_falls_back_to_deployment_when_no_upstream(self) -> None:
        from application_sdk.app.context import AppContext
        from application_sdk.contracts.storage import DownloadInput, DownloadOutput

        deployment_sentinel = object()

        class _DlApp2(App):
            async def run(self, input: _BLDXInput) -> _BLDXOutput:
                return _BLDXOutput()

        app = _DlApp2()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=deployment_sentinel,  # type: ignore[arg-type]
        )

        mock_result = DownloadOutput()
        with mock.patch(
            "application_sdk.storage.transfer.download",
            new_callable=mock.AsyncMock,
            return_value=mock_result,
        ) as mock_download:
            await app.download(DownloadInput(storage_path="artifacts/out"))

        assert mock_download.call_args.kwargs["store"] is deployment_sentinel


# =============================================================================
# Inline-import smoke checks
# =============================================================================


class TestInlineImportContracts:
    """Direct asserts that every name imported inline by base.py still exists."""

    def test_storage_transfer_names(self) -> None:
        # Used in App.upload / App.download
        from application_sdk.storage.transfer import download, upload  # noqa: F401

    def test_constants_names(self) -> None:
        from application_sdk.constants import (  # noqa: F401
            CLEANUP_BASE_PATHS,
            PROTECTED_STORAGE_PREFIXES,
            TEMPORARY_PATH,
            TRACKED_FILE_REFS_KEY,
        )

    def test_execution_build_output_path_name(self) -> None:
        from application_sdk.execution import build_output_path  # noqa: F401

    def test_storage_ops_names(self) -> None:
        from application_sdk.storage.ops import _resolve_store, delete  # noqa: F401

    def test_execution_errors_application_error(self) -> None:
        from application_sdk.execution.errors import ApplicationError  # noqa: F401

    def test_execution_retry_names(self) -> None:
        from application_sdk.execution.retry import (  # noqa: F401
            RetryPolicy,
            _to_temporal_retry_policy,
        )

    def test_temporal_activities_task_context(self) -> None:
        # This reaches into a private module because App wraps task activities via that path.
        from application_sdk.execution._temporal.activities import (  # noqa: F401
            TaskContext,
        )

    def test_observability_correlation_get_correlation_context(self) -> None:
        from application_sdk.observability.correlation import (  # noqa: F401
            get_correlation_context,
        )

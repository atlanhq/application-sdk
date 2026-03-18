"""Unit tests for app and handler discovery."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.discovery import (
    DiscoveryError,
    _parse_module_path,
    load_app_class,
    load_handler_class,
    validate_app_class,
)
from application_sdk.handler.base import Handler


@dataclass
class _DiscInput(Input):
    name: str = ""


@dataclass
class _DiscOutput(Output):
    result: str = ""


class TestParseModulePath:
    """Tests for _parse_module_path()."""

    def test_valid_format(self) -> None:
        module_name, class_name = _parse_module_path("pkg.mod:Cls")
        assert module_name == "pkg.mod"
        assert class_name == "Cls"

    def test_no_colon_raises(self) -> None:
        with pytest.raises(DiscoveryError):
            _parse_module_path("pkgmod")

    def test_empty_module_name_raises(self) -> None:
        with pytest.raises(DiscoveryError):
            _parse_module_path(":ClassName")

    def test_empty_class_name_raises(self) -> None:
        with pytest.raises(DiscoveryError):
            _parse_module_path("pkg.mod:")

    def test_nested_module_path(self) -> None:
        module_name, class_name = _parse_module_path("a.b.c.d:MyClass")
        assert module_name == "a.b.c.d"
        assert class_name == "MyClass"

    def test_class_name_with_colon_uses_first_split(self) -> None:
        # Only split on first colon
        module_name, class_name = _parse_module_path("pkg:Cls:Extra")
        assert module_name == "pkg"
        assert class_name == "Cls:Extra"


class TestDiscoveryErrorStr:
    """Tests for DiscoveryError.__str__."""

    def test_basic_message(self) -> None:
        err = DiscoveryError("Something failed")
        assert "Something failed" in str(err)

    def test_includes_module_path(self) -> None:
        err = DiscoveryError("Failed", module_path="my.module:MyClass")
        result = str(err)
        assert "my.module:MyClass" in result

    def test_includes_cause_type(self) -> None:
        cause = ImportError("no module")
        err = DiscoveryError("Failed", cause=cause)
        result = str(err)
        assert "ImportError" in result

    def test_all_fields(self) -> None:
        cause = ValueError("bad value")
        err = DiscoveryError("msg", module_path="mod:Cls", cause=cause)
        result = str(err)
        assert "msg" in result
        assert "mod:Cls" in result
        assert "ValueError" in result


class TestLoadAppClass:
    """Tests for load_app_class()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_loads_valid_app_class(self) -> None:
        # Create a temporary module with an App subclass
        mod_name = "_test_discovery_app_module"
        mod = types.ModuleType(mod_name)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        class _MyDiscApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        _MyDiscApp.__module__ = mod_name
        mod.MyDiscApp = _MyDiscApp  # type: ignore[attr-defined]
        sys.modules[mod_name] = mod

        try:
            result = load_app_class(f"{mod_name}:MyDiscApp")
            assert result is _MyDiscApp
        finally:
            del sys.modules[mod_name]

    def test_not_app_subclass_raises(self) -> None:
        mod_name = "_test_discovery_not_app"
        mod = types.ModuleType(mod_name)

        class NotAnApp:
            pass

        mod.NotAnApp = NotAnApp  # type: ignore[attr-defined]
        sys.modules[mod_name] = mod

        try:
            with pytest.raises(DiscoveryError):
                load_app_class(f"{mod_name}:NotAnApp")
        finally:
            del sys.modules[mod_name]

    def test_module_not_found_raises(self) -> None:
        with pytest.raises(DiscoveryError) as exc_info:
            load_app_class("nonexistent.module.xyz:SomeApp")
        assert exc_info.value.cause is not None
        assert isinstance(exc_info.value.cause, ImportError)

    def test_invalid_module_path_raises(self) -> None:
        with pytest.raises(DiscoveryError):
            load_app_class("nocolon")


class TestLoadHandlerClass:
    """Tests for load_handler_class()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_app_module(
        self,
        mod_name: str,
        app_class_name: str,
        handler_class_name: str | None = None,
    ) -> types.ModuleType:
        mod = types.ModuleType(mod_name)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        # Create a proper App subclass
        class _ConcreteApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        _ConcreteApp.__name__ = app_class_name
        _ConcreteApp.__qualname__ = app_class_name
        _ConcreteApp.__module__ = mod_name
        setattr(mod, app_class_name, _ConcreteApp)

        if handler_class_name:

            class _ConcreteHandler(Handler):
                async def test_auth(self, input):  # type: ignore
                    pass

                async def preflight_check(self, input):  # type: ignore
                    pass

                async def fetch_metadata(self, input):  # type: ignore
                    pass

            _ConcreteHandler.__name__ = handler_class_name
            _ConcreteHandler.__qualname__ = handler_class_name
            _ConcreteHandler.__module__ = mod_name
            setattr(mod, handler_class_name, _ConcreteHandler)

        sys.modules[mod_name] = mod
        return mod

    def test_explicit_handler_path(self) -> None:
        app_mod = "_test_disc_handler_explicit_app"
        handler_mod = "_test_disc_handler_explicit_handler"

        app_module = types.ModuleType(app_mod)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        class _AppForHandler(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        _AppForHandler.__module__ = app_mod
        app_module.MyApp = _AppForHandler  # type: ignore[attr-defined]

        handler_module = types.ModuleType(handler_mod)

        class _ExplicitHandler(Handler):
            pass

        _ExplicitHandler.__module__ = handler_mod
        handler_module.MyExplicitHandler = _ExplicitHandler  # type: ignore[attr-defined]

        sys.modules[app_mod] = app_module
        sys.modules[handler_mod] = handler_module

        try:
            result = load_handler_class(
                f"{app_mod}:MyApp",
                handler_module_path=f"{handler_mod}:MyExplicitHandler",
            )
            assert result is _ExplicitHandler
        finally:
            del sys.modules[app_mod]
            del sys.modules[handler_mod]

    def test_convention_based_discovery(self) -> None:
        mod_name = "_test_disc_convention"
        mod = types.ModuleType(mod_name)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        class ConventionApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        class ConventionAppHandler(Handler):
            pass

        ConventionApp.__module__ = mod_name
        ConventionAppHandler.__module__ = mod_name
        mod.ConventionApp = ConventionApp  # type: ignore[attr-defined]
        mod.ConventionAppHandler = ConventionAppHandler  # type: ignore[attr-defined]
        sys.modules[mod_name] = mod

        try:
            result = load_handler_class(f"{mod_name}:ConventionApp")
            assert result is ConventionAppHandler
        finally:
            del sys.modules[mod_name]

    def test_fallback_scan_finds_handler_subclass(self) -> None:
        mod_name = "_test_disc_fallback"
        mod = types.ModuleType(mod_name)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        class FallbackApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        class CustomNamedHandler(Handler):
            pass

        FallbackApp.__module__ = mod_name
        CustomNamedHandler.__module__ = mod_name
        mod.FallbackApp = FallbackApp  # type: ignore[attr-defined]
        mod.CustomNamedHandler = CustomNamedHandler  # type: ignore[attr-defined]
        sys.modules[mod_name] = mod

        try:
            result = load_handler_class(f"{mod_name}:FallbackApp")
            # Should find CustomNamedHandler by scan since FallbackAppHandler doesn't exist
            assert result is CustomNamedHandler
        finally:
            del sys.modules[mod_name]

    def test_returns_none_when_no_handler_found(self) -> None:
        mod_name = "_test_disc_no_handler"
        mod = types.ModuleType(mod_name)

        @dataclass
        class _In(Input):
            x: str = ""

        @dataclass
        class _Out(Output):
            y: str = ""

        class NoHandlerApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        NoHandlerApp.__module__ = mod_name
        mod.NoHandlerApp = NoHandlerApp  # type: ignore[attr-defined]
        sys.modules[mod_name] = mod

        try:
            result = load_handler_class(f"{mod_name}:NoHandlerApp")
            assert result is None
        finally:
            del sys.modules[mod_name]


@dataclass
class _ValidateIn(Input):
    x: str = ""


@dataclass
class _ValidateOut(Output):
    y: str = ""


class TestValidateAppClass:
    """Tests for validate_app_class()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_valid_registered_app_passes(self) -> None:
        # Class defined inside test using module-level types so get_type_hints resolves them
        class ValidatedApp(App):
            async def run(self, input: _ValidateIn) -> _ValidateOut:
                return _ValidateOut()

        # Should not raise — the class was just registered
        validate_app_class(ValidatedApp)

    def test_non_app_class_raises(self) -> None:
        class NotAnApp:
            pass

        with pytest.raises(DiscoveryError):
            validate_app_class(NotAnApp)  # type: ignore[arg-type]

    def test_unregistered_app_raises(self) -> None:
        class SomeOtherApp(App):
            async def run(self, input: _ValidateIn) -> _ValidateOut:
                return _ValidateOut()

        # Reset the registry so the app is no longer registered
        AppRegistry.reset()
        TaskRegistry.reset()

        # _app_name is still set on the class, but it's not in the registry
        with pytest.raises(DiscoveryError):
            validate_app_class(SomeOtherApp)

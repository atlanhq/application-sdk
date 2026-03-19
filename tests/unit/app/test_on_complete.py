"""Tests for App.on_complete() lifecycle hook."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import CleanupInput, CleanupOutput


@dataclass
class _OCInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _OCOutput(Output, allow_unbounded_fields=True):
    result: str = ""


class TestOnComplete:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    @pytest.mark.asyncio
    async def test_default_calls_cleanup_files(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m:
            m.return_value = CleanupOutput()
            await app.on_complete()

        m.assert_called_once_with(CleanupInput())

    @pytest.mark.asyncio
    async def test_cleanup_disabled_via_env_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m:
            await app.on_complete()

        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_disabled_via_env_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "0")

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m:
            await app.on_complete()

        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_propagate(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()

        with mock.patch(
            "application_sdk.app.base._safe_log"
        ):
            with mock.patch.object(
                app, "cleanup_files", new_callable=mock.AsyncMock
            ) as m:
                m.side_effect = RuntimeError("cleanup boom")
                # Should not raise — on_complete() swallows cleanup errors
                await app.on_complete()

    @pytest.mark.asyncio
    async def test_subclass_override_adds_custom_logic(self) -> None:
        custom_called: list[bool] = []

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

            async def on_complete(self) -> None:
                custom_called.append(True)
                await super().on_complete()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m:
            m.return_value = CleanupOutput()
            await app.on_complete()

        assert custom_called == [True]
        m.assert_called_once()

    @pytest.mark.asyncio
    async def test_subclass_can_skip_super(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

            async def on_complete(self) -> None:
                pass  # deliberately skip super()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m:
            await app.on_complete()

        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_not_yet_nulled_during_on_complete(self) -> None:
        """on_complete() is called before self._context is nulled."""
        from application_sdk.app.context import AppContext

        context_during_complete: list[AppContext | None] = []

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

            async def on_complete(self) -> None:
                context_during_complete.append(self._context)

        app = _App()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        app._context = ctx
        await app.on_complete()

        assert context_during_complete == [ctx]

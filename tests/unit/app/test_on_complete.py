"""Tests for App.on_complete() lifecycle hook."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import (
    CleanupInput,
    CleanupOutput,
    StorageCleanupInput,
    StorageCleanupOutput,
)


@dataclass
class _OCInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _OCOutput(Output, allow_unbounded_fields=True):
    result: str = ""


class TestOnComplete:
    async def test_default_calls_cleanup_files(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(
            app, "cleanup_files", new_callable=mock.AsyncMock
        ) as m_files:
            m_files.return_value = CleanupOutput()
            with mock.patch.object(
                app, "cleanup_storage", new_callable=mock.AsyncMock
            ) as m_storage:
                m_storage.return_value = StorageCleanupOutput()
                await app.on_complete()

        m_files.assert_called_once_with(CleanupInput())

    async def test_default_passes_the_runs_stored_refs_to_cleanup_storage(
        self,
    ) -> None:
        """FND-5220: refs the workflow read off its tasks reach cleanup_storage."""
        from application_sdk.app.base import _RunStoredRefs
        from application_sdk.contracts.types import FileReference

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        app._run_stored_refs = _RunStoredRefs()
        app._run_stored_refs.record(
            [FileReference(local_path="/a/x", storage_path="file_refs/x")]
        )
        with (
            mock.patch.object(app, "cleanup_files", new_callable=mock.AsyncMock),
            mock.patch.object(
                app, "cleanup_storage", new_callable=mock.AsyncMock
            ) as m_storage,
        ):
            await app.on_complete()

        m_storage.assert_called_once_with(
            StorageCleanupInput(
                file_refs=[
                    FileReference(storage_path="file_refs/x", auto_materialize=False)
                ]
            )
        )

    async def test_default_calls_both_cleanups_concurrently(self) -> None:
        """on_complete() runs cleanup_files and cleanup_storage concurrently."""
        import asyncio

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        call_order: list[str] = []

        async def _files_side_effect(*args: object, **kwargs: object) -> CleanupOutput:
            call_order.append("files_start")
            await asyncio.sleep(0)  # yield to allow storage to start
            call_order.append("files_end")
            return CleanupOutput()

        async def _storage_side_effect(
            *args: object, **kwargs: object
        ) -> StorageCleanupOutput:
            call_order.append("storage_start")
            await asyncio.sleep(0)
            call_order.append("storage_end")
            return StorageCleanupOutput()

        with mock.patch.object(
            app, "cleanup_files", side_effect=_files_side_effect
        ) as m_files:
            with mock.patch.object(
                app, "cleanup_storage", side_effect=_storage_side_effect
            ) as m_storage:
                await app.on_complete()

        m_files.assert_called_once_with(CleanupInput())
        m_storage.assert_called_once_with(StorageCleanupInput())
        # Both were started before either finished (interleaved, not sequential)
        assert "files_start" in call_order
        assert "storage_start" in call_order

    async def test_cleanup_disabled_via_env_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(app, "cleanup_files", new_callable=mock.AsyncMock) as m:
            await app.on_complete()

        m.assert_not_called()

    async def test_cleanup_disabled_via_env_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "0")

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        with mock.patch.object(app, "cleanup_files", new_callable=mock.AsyncMock) as m:
            await app.on_complete()

        m.assert_not_called()

    @pytest.mark.parametrize(
        ("env_value", "pinned_cleanup"), [("false", True), ("true", False)]
    )
    async def test_a_run_s_pinned_cleanup_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, env_value: str, pinned_cleanup: bool
    ) -> None:
        """Inside a run, the value pinned at the run's first workflow task
        decides; the pod's environment does not, so a replay on a pod with the
        other value schedules the same cleanup activities."""
        from application_sdk.execution._temporal.run_flags import _RUN_FLAGS, RunFlags

        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", env_value)

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()
        token = _RUN_FLAGS.set(RunFlags(events=True, cleanup=pinned_cleanup))
        try:
            with mock.patch.object(
                app, "cleanup_files", new_callable=mock.AsyncMock
            ) as m:
                await app.on_complete()
        finally:
            _RUN_FLAGS.reset(token)

        assert m.called is pinned_cleanup

    async def test_cleanup_failure_does_not_propagate(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

        app = _App()

        with mock.patch("application_sdk.app.base._safe_log") as safe_log:
            with mock.patch.object(
                app, "cleanup_files", new_callable=mock.AsyncMock
            ) as m:
                m.side_effect = RuntimeError("cleanup boom")
                # Should not raise — on_complete() swallows cleanup errors
                await app.on_complete()

        m.assert_awaited_once()
        safe_log.assert_any_call(
            "warning", "cleanup_files task failed during on_complete", exc_info=True
        )

    async def test_subclass_override_adds_custom_logic(self) -> None:
        custom_called: list[bool] = []

        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

            async def on_complete(self) -> None:
                custom_called.append(True)
                await super().on_complete()

        app = _App()
        with mock.patch.object(app, "cleanup_files", new_callable=mock.AsyncMock) as m:
            m.return_value = CleanupOutput()
            await app.on_complete()

        assert custom_called == [True]
        m.assert_called_once()

    async def test_subclass_can_skip_super(self) -> None:
        class _App(App):
            async def run(self, input: _OCInput) -> _OCOutput:
                return _OCOutput()

            async def on_complete(self) -> None:
                pass  # deliberately skip super()

        app = _App()
        with mock.patch.object(app, "cleanup_files", new_callable=mock.AsyncMock) as m:
            await app.on_complete()

        m.assert_not_called()

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

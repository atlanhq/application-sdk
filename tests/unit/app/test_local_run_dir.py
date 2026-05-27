"""Tests for App.local_run_dir() — run-scoped local scratch root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, AppContextError, _app_state, _app_state_lock
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output


@dataclass
class _LRInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _LROutput(Output, allow_unbounded_fields=True):
    result: str = ""


class _LRApp(App):
    async def run(self, input: _LRInput) -> _LROutput:
        return _LROutput()


def _info(workflow_id: str, run_id: str) -> Any:
    return mock.Mock(workflow_id=workflow_id, workflow_run_id=run_id)


class TestLocalRunDir:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        with _app_state_lock:
            _app_state.clear()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        with _app_state_lock:
            _app_state.clear()

    def test_returns_run_scoped_path_and_creates_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.LOCAL_FILE_REF_ROOT", str(tmp_path)
        )
        app = _LRApp()
        with mock.patch(
            "application_sdk.app.base.activity.info",
            return_value=_info("wf1", "run1"),
        ):
            path = app.local_run_dir()

        assert path == tmp_path / "wf1" / "run1"
        assert path.is_dir()

    def test_sanitizes_slashes_in_workflow_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.LOCAL_FILE_REF_ROOT", str(tmp_path)
        )
        app = _LRApp()
        with mock.patch(
            "application_sdk.app.base.activity.info",
            return_value=_info("ns/team/wf-7", "run-1"),
        ):
            path = app.local_run_dir()

        # The slash-bearing workflow_id collapses to a single safe segment —
        # no accidental nested directories.
        assert path == tmp_path / "ns_team_wf-7" / "run-1"
        assert path.is_dir()
        assert not (tmp_path / "ns").exists()

    def test_raises_app_context_error_when_ids_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.LOCAL_FILE_REF_ROOT", str(tmp_path)
        )
        app = _LRApp()
        # Outside a real activity context Temporal's Info ids are None — surface a
        # clear AppContextError rather than a raw TypeError from path building.
        with (
            mock.patch(
                "application_sdk.app.base.activity.info",
                return_value=_info(None, "run1"),
            ),
            pytest.raises(AppContextError),
        ):
            app.local_run_dir()

    def test_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.LOCAL_FILE_REF_ROOT", str(tmp_path)
        )
        app = _LRApp()
        with mock.patch(
            "application_sdk.app.base.activity.info",
            return_value=_info("wf1", "run1"),
        ):
            first = app.local_run_dir()
            (first / "scratch.json").write_text("{}")
            second = app.local_run_dir()

        assert first == second
        # Re-creating must not wipe existing scratch.
        assert (second / "scratch.json").exists()

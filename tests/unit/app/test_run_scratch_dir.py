"""FND-5364: ``App.run_scratch_dir`` — the run's scratch dir, from the run's context."""

from __future__ import annotations

from collections.abc import Iterator
from unittest import mock

import pytest

from application_sdk.app._run_scratch import run_scratch_dir
from application_sdk.app.base import App, AppContextError
from application_sdk.app.context import AppContext
from application_sdk.app.registry import _running_app_name
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal import activity_utils
from application_sdk.execution._temporal._activity_errors import (
    RunTempDirEscapeError,
)
from application_sdk.testing.fixtures import _redirect_temporary_path


class _ScratchInput(Input):
    pass


class _ScratchOutput(Output):
    pass


class ScratchDirApp(App):
    name = "scratch-dir-app"

    async def run(self, input: _ScratchInput) -> _ScratchOutput:
        return _ScratchOutput()


def _app_with(workflow_id: str, run_id: str) -> ScratchDirApp:
    app = ScratchDirApp()
    app._context = AppContext(
        app_name=ScratchDirApp._app_name,
        app_version="1.0.0",
        run_id=run_id,
        workflow_id=workflow_id,
    )
    return app


@pytest.fixture(autouse=True)
def _tmp_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[str]:
    root = str(tmp_path / "sdk-tmp")
    _redirect_temporary_path(monkeypatch, root)
    yield root


def test_property_is_the_run_prefix_under_temporary_path(_tmp_root: str) -> None:
    app = _app_with("wf-123", "run-456")
    assert app.run_scratch_dir == (
        f"{_tmp_root}/artifacts/apps/scratch-dir-app/workflows/wf-123/run-456"
    )


def test_property_equals_run_temp_dir_of_a_task_of_the_same_run() -> None:
    """The workflow's path is the one ``cleanup_files`` removes from a task."""
    app = _app_with("wf-123", "run-456")
    token = _running_app_name.set(ScratchDirApp._app_name)
    try:
        with (
            mock.patch.object(activity_utils, "get_workflow_id", return_value="wf-123"),
            mock.patch.object(
                activity_utils, "get_workflow_run_id", return_value="run-456"
            ),
        ):
            assert app.run_scratch_dir == activity_utils.run_temp_dir()
    finally:
        _running_app_name.reset(token)


def test_property_outside_a_run_raises_app_context_error() -> None:
    with pytest.raises(AppContextError):
        _ = ScratchDirApp().run_scratch_dir


@pytest.mark.parametrize(
    "workflow_id",
    [
        # Four segments (artifacts/apps/<app>/workflows) precede the workflow
        # ID, so five ``..`` leave the root; fewer land back inside it.
        "../../../../../outside",
        "../../../../..",
        "../../../../../../../../etc",
    ],
)
def test_a_workflow_id_escaping_temporary_path_raises(workflow_id: str) -> None:
    with pytest.raises(RunTempDirEscapeError):
        _ = _app_with(workflow_id, "run-1").run_scratch_dir


@pytest.mark.parametrize("workflow_id", ["../other-wf", "../../..", "../../../.."])
def test_dotdot_that_stays_inside_is_allowed(workflow_id: str, _tmp_root: str) -> None:
    """``..`` segments that land back inside ``TEMPORARY_PATH`` are not an escape.

    The same verdict ``run_temp_dir()``'s realpath check reaches for them.
    """
    path = run_scratch_dir("app", workflow_id, "run")
    assert path.startswith(_tmp_root)


def test_the_root_itself_is_refused() -> None:
    """A prefix that normalises to ``TEMPORARY_PATH`` would sweep every run."""
    with pytest.raises(RunTempDirEscapeError):
        run_scratch_dir("app", "../../../..", ".")


def test_no_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workflow-safe: computing the path reads neither the filesystem nor cwd."""
    import os

    def _refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("filesystem access")

    for name in ("stat", "lstat", "getcwd", "listdir", "scandir"):
        monkeypatch.setattr(os, name, _refuse)
    assert run_scratch_dir("app", "wf", "run").endswith(
        "artifacts/apps/app/workflows/wf/run"
    )

"""``App.run_scratch_dir`` in workflow code, a task, and cleanup (FND-5364).

The worker is built by ``create_worker`` with no extra passthrough modules
(the shared ``run_worker`` fixture passes all of ``tests``). Until #5056
(FND-5218) lands, ``create_worker`` still passes every registered app module
through the sandbox, so on its own this checks the shared path and the
cleanup, not the sandbox. With #5056 the app module is re-imported in the
sandbox, as a consumer app's is in production, and ``run()`` reads the
property as checked workflow code: verified on main + #5056 on Python 3.13,
where the same app with ``tempfile.gettempdir()`` in ``run()`` fails its
workflow task.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from application_sdk.app.context import AppContext
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.execution.retry import NO_RETRY
from application_sdk.testing.fixtures import _redirect_temporary_path
from tests.integration._run_scratch_apps import RunScratchDirApp, ScratchInput

# Long enough for a cold sandbox import of the app module on a slow runner.
_DEADLINE_SECONDS = 60


@pytest.mark.integration
async def test_workflow_and_task_share_the_dir_and_cleanup_removes_it(
    temporal_client, task_queue, executor, reregister_app, monkeypatch, tmp_path
):
    tmp_root = str(tmp_path / "sdk-tmp")
    _redirect_temporary_path(monkeypatch, tmp_root)
    reregister_app(RunScratchDirApp)
    worker = create_worker(temporal_client, task_queue, shutdown_drain_delay_seconds=0)
    async with worker:
        result = await asyncio.wait_for(
            executor.execute(
                RunScratchDirApp,
                ScratchInput(),
                context=AppContext(
                    app_name=RunScratchDirApp._app_name, app_version="1.0.0"
                ),
                retry_policy=NO_RETRY,
            ),
            timeout=_DEADLINE_SECONDS,
        )

    expected = os.path.normpath(
        os.path.join(tmp_root, "artifacts", "apps", RunScratchDirApp._app_name)
    )
    workflow_dir = os.path.normpath(result.workflow_dir)
    assert workflow_dir.startswith(expected + os.sep), result.workflow_dir
    assert result.workflow_dir == result.task_dir
    assert os.path.normpath(result.wrote).startswith(workflow_dir + os.sep)
    # cleanup_files (on_complete) removed the directory the workflow named.
    assert not os.path.exists(result.wrote)
    assert not os.path.exists(result.workflow_dir)

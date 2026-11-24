"""SDK-internal: the local scratch directory of one workflow run.

The one place the run scratch directory is composed from a run's identity
(app name, workflow ID, run ID). ``App.run_scratch_dir`` calls it with the
run's ``AppContext`` (in workflow code and in tasks alike), and
``run_temp_dir()`` calls it with the IDs it reads from ``activity.info()``, so
the directory a workflow names is the one ``cleanup_files`` removes.

Workflow-safe by construction: only the string-only ``os.path`` members
(``join``, ``normpath``, ``isabs``) are used, the same ones the workflow
sandbox allows on every Python version. Nothing here reads the filesystem, the
working directory, the clock or ``tempfile``, so the result is the same on
replay on any worker.
"""

from __future__ import annotations

import os

from application_sdk import constants
from application_sdk.constants import _run_prefix


def run_scratch_dir(app_name: str, workflow_id: str, run_id: str) -> str:
    """Return ``{TEMPORARY_PATH}/{run prefix}`` for the run, checked to stay inside.

    ``TEMPORARY_PATH`` is read at call time so a redirect of the constant is
    honoured. The workflow ID is caller-chosen (``/workflows/v1/start``
    accepts it verbatim), so the run prefix is checked lexically to resolve
    strictly inside ``TEMPORARY_PATH``: not outside it via ``..`` segments, not
    absolute, and not the root itself.

    Args:
        app_name: The registered name of the App that owns the run.
        workflow_id: The run's workflow ID.
        run_id: The run's Temporal run ID.

    Returns:
        The directory, e.g.
        ``./local/tmp/artifacts/apps/my-app/workflows/wf-123/run-456``.

    Raises:
        RunTempDirEscapeError: If the directory would not be strictly inside
            ``TEMPORARY_PATH``.
    """
    from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
        RunTempDirEscapeError,
    )

    prefix = _run_prefix(app_name, workflow_id, run_id)
    rel = os.path.normpath(prefix)
    if (
        os.path.isabs(rel)
        or rel in (os.curdir, os.pardir)
        or rel.startswith(os.pardir + os.sep)
    ):
        raise RunTempDirEscapeError()
    return os.path.join(constants.TEMPORARY_PATH, prefix)

"""The current task's run, and the credential directories it made on this worker.

A leaf of ``execution._temporal``: it imports nothing from ``application_sdk.app``
or ``application_sdk.credentials``. ``credentials.utils`` registers each
directory here from inside a task, and ``app.base`` (``cleanup_files``) and the
worker's sweeper (``credential_sweep``) remove them, so none of the three has
to import another to reach the registry.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable

from temporalio import activity

from application_sdk._context_errors import AppContextError
from application_sdk._runtime.offload import run_in_thread
from application_sdk.observability.logger_adaptor import get_logger

__all__: list[str] = []

logger = get_logger(__name__)

#: Credential file directories this worker process wrote, by the run that
#: wrote them: ``(workflow_id, run_id) -> paths``. Process-wide, not in the
#: run's app-state slot, because the files sit on this worker's disk while the
#: run's ``cleanup_files`` may run on another worker and its state slot is
#: pruned on whichever worker ran the workflow. Whatever the run's own cleanup
#: does not reach, the worker's sweeper removes once Temporal reports the run
#: closed (``execution._temporal.credential_sweep``), and the rest at worker
#: shutdown.
_credential_dirs_by_run: dict[tuple[str, str], set[str]] = {}
_credential_dirs_lock = threading.Lock()

#: What to call once a tracked directory is removed, by path: the tracker's
#: ``forget`` callback (``credentials.utils`` drops the path from its issued
#: set), passed in so this module never imports the credentials package.
_on_removed: dict[str, Callable[[str], None]] = {}


def _current_run_key() -> tuple[str, str]:
    """``(workflow_id, run_id)`` of the run the current task belongs to.

    Raises:
        AppContextError: If called outside a task.
    """
    # One guarded probe for both ids: outside an activity, temporalio raises a
    # bare RuntimeError, which must surface as the AppContextError documented
    # here and on get_app_state / set_app_state.
    try:
        info = activity.info()
        workflow_id = info.workflow_id
        run_id = info.workflow_run_id
    # conformance: ignore[E004] probe for Temporal activity context; broad catch is intentional, exception re-raised as typed AppContextError
    except Exception as e:
        raise AppContextError("Cannot access app state outside of task context") from e
    if not workflow_id:
        raise AppContextError("activity workflow_id is empty")
    if not run_id:
        raise AppContextError("activity workflow_run_id is empty")
    return workflow_id, run_id


def _track_credential_dir(
    path: str, forget: Callable[[str], None] | None = None
) -> None:
    """SDK-internal: register a credential file directory with the current run.

    Called by ``credentials.utils`` from inside a task for each private
    directory it makes. The run's ``cleanup_files`` removes it when it runs
    on this worker, whatever ``extra_paths`` or ``CLEANUP_BASE_PATHS`` say;
    otherwise this worker's sweeper does once the run has closed, so a keytab
    or private key does not outlive the run on a long-lived worker.

    Args:
        path: The directory.
        forget: Called with *path* once the directory has been removed.

    Raises:
        AppContextError: If called outside a task.
    """
    run = _current_run_key()
    with _credential_dirs_lock:
        _credential_dirs_by_run.setdefault(run, set()).add(path)
        if forget is not None:
            _on_removed[path] = forget


def _runs_with_credential_dirs() -> list[tuple[str, str]]:
    """The runs that still have credential directories on this worker."""
    with _credential_dirs_lock:
        return list(_credential_dirs_by_run)


async def _remove_run_credential_dirs(run: tuple[str, str]) -> dict[str, bool]:
    """Remove *run*'s credential directories on this worker.

    Returns each directory's outcome. A removed one is untracked and handed to
    its ``forget`` callback; one that could not be removed is logged and stays
    tracked, so a later cleanup or sweep retries it.
    """
    with _credential_dirs_lock:
        dirs = sorted(_credential_dirs_by_run.get(run, ()))
    results: dict[str, bool] = {}
    for path in dirs:
        try:
            if os.path.lexists(path):
                await run_in_thread(shutil.rmtree, path)
            results[path] = True
        except OSError:
            logger.warning("Failed to delete credential file directory", exc_info=True)
            results[path] = False
    removed = [path for path, ok in results.items() if ok]
    forgets: list[tuple[str, Callable[[str], None]]] = []
    with _credential_dirs_lock:
        remaining = _credential_dirs_by_run.get(run)
        if remaining is not None:
            remaining.difference_update(removed)
            if not remaining:
                del _credential_dirs_by_run[run]
        for path in removed:
            forget = _on_removed.pop(path, None)
            if forget is not None:
                forgets.append((path, forget))
    for path, forget in forgets:
        forget(path)
    return results

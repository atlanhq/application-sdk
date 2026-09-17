"""ADR-0018: which framework tasks may declare a duration, and which may not.

``timeout_seconds`` is a **backstop, not a duration budget**. A per-task number
is only defensible where the task's duration does not scale with tenant size —
and where it does scale, the replacement is not a bigger number, it is the stall
watchdog reading progress the work already emits.

That split is the whole content of this file, and it is asymmetric on purpose:

* ``upload`` / ``download`` / ``upload_refs`` / ``verify_refs`` move or check an
  amount of data the caller chooses, so any number here is a moving target. All
  four emit progress (per file, per multipart part, per range chunk, per checked
  ref), so the watchdog can bound a wedge in minutes and the backstop is the
  last resort.
* ``cleanup_files`` / ``cleanup_storage`` emit **nothing**, and
  ``cleanup_storage`` has heartbeating disabled outright. For them the duration
  is the only bound there is, so removing it would trade a 300s kill for a
  silent hold — the opposite of the trade the ADR makes.

The two lists below are the pin. A task moving between them should be a
deliberate edit with a progress hook attached, not a drive-by.
"""

from __future__ import annotations

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import _DEFAULT_TIMEOUT_SECONDS, get_task_metadata

#: Duration scales with tenant size → backstop only, watchdog does the bounding.
BACKSTOP_TASKS = ("upload", "download", "verify_refs", "upload_refs")

#: No progress signal → the declared duration is the only bound. Keep it.
DECLARED_DURATION_TASKS = ("cleanup_files", "cleanup_storage")


@pytest.mark.parametrize("name", BACKSTOP_TASKS)
def test_data_moving_tasks_take_the_backstop(name: str) -> None:
    meta = get_task_metadata(getattr(App, name))
    assert meta is not None
    assert meta.timeout_seconds == _DEFAULT_TIMEOUT_SECONDS, (
        f"App.{name} declares a per-task duration. Its runtime scales with the "
        "amount of data the caller passes, so any value is a moving target "
        "(ADR-0018 Problem 1) — take the backstop and let the stall watchdog "
        "bound a wedge instead."
    )


@pytest.mark.parametrize("name", BACKSTOP_TASKS)
def test_data_moving_tasks_still_heartbeat(name: str) -> None:
    """The backstop is only acceptable because something smaller is watching.

    A task on the 24h backstop with heartbeating off has no bound anyone would
    notice inside a day.
    """
    meta = get_task_metadata(getattr(App, name))
    assert meta is not None
    assert meta.heartbeat_timeout_seconds is not None, (
        f"App.{name} is on the backstop with heartbeating disabled — nothing "
        "would catch a wedge before 24h"
    )
    assert meta.auto_heartbeat_seconds is not None


@pytest.mark.parametrize("name", DECLARED_DURATION_TASKS)
def test_progressless_tasks_keep_their_declared_duration(name: str) -> None:
    meta = get_task_metadata(getattr(App, name))
    assert meta is not None
    assert meta.timeout_seconds != _DEFAULT_TIMEOUT_SECONDS, (
        f"App.{name} emits no progress, so the watchdog cannot bound it. "
        "Dropping its duration trades a bounded kill for a silent hold. Add a "
        "progress hook first, then move it to BACKSTOP_TASKS."
    )


def test_the_two_lists_cover_every_framework_task() -> None:
    """A new framework task must land in one list or the other.

    Without this, adding a task with a hand-picked duration passes silently and
    the ADR erodes one decorator at a time.
    """
    declared = {
        name
        for name in dir(App)
        if not name.startswith("__")
        and get_task_metadata(getattr(App, name, None)) is not None
    }
    assert declared == set(BACKSTOP_TASKS) | set(DECLARED_DURATION_TASKS), (
        "framework @task set changed — classify the new task by whether it "
        "emits progress, then add it to the matching list above"
    )

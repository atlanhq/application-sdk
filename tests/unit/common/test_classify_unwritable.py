"""The activity boundary types a read-only / permission-denied local-write OSError.

`atomic.py`'s `disk_full_guard` deliberately stays narrow to ENOSPC/EDQUOT (see
`test_an_unrelated_oserror_is_not_reclassified`). The artifact-filesystem failure
family — `[Errno 30] Read-only file system: 'artifacts'` and siblings — is instead
typed at the activity boundary via `classify_unwritable_oserror`, so no write-path
invariant changes. These tests pin that classifier.

The classifier is called from the outermost `except Exception` in
`create_activity_from_task`, which sees every non-`AppError` raised anywhere in an
activity body. So the interesting half of its contract is what it *refuses*: an
`EACCES` on a path the app does not own must stay unclassified rather than be
confidently misattributed to the volume mount. `TestWriteProvenanceIsRequired`
pins that, and `TestActivityBoundaryTranslatesIt` pins the wiring that the
isolated tests cannot see.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest import mock

import pytest

from application_sdk.common.atomic import classify_unwritable_oserror
from application_sdk.contracts.base import Input, Output
from application_sdk.errors import LocalVolumeUnwritableError


class TestClassifyUnwritableOSError:
    def test_erofs_read_only_filesystem_becomes_typed(self) -> None:
        err = classify_unwritable_oserror(
            OSError(errno.EROFS, "Read-only file system", "artifacts")
        )
        assert isinstance(err, LocalVolumeUnwritableError)
        assert err.errno_name == "EROFS"
        assert err.path == "artifacts"

    def test_eacces_permission_denied_becomes_typed(self) -> None:
        err = classify_unwritable_oserror(
            OSError(errno.EACCES, "Permission denied", "artifacts/apps")
        )
        assert isinstance(err, LocalVolumeUnwritableError)
        assert err.errno_name == "EACCES"

    def test_eperm_operation_not_permitted_becomes_typed(self) -> None:
        # EPERM is in _UNWRITABLE_ERRNOS and in the docstring's contract; without
        # this case it could be dropped from the frozenset with nothing failing.
        err = classify_unwritable_oserror(
            PermissionError(errno.EPERM, "Operation not permitted", "artifacts/apps")
        )
        assert isinstance(err, LocalVolumeUnwritableError)
        assert err.errno_name == "EPERM"

    def test_the_temporary_path_root_is_covered_too(self) -> None:
        # Both local write roots count as provenance, not just `artifacts/`.
        err = classify_unwritable_oserror(
            OSError(errno.EROFS, "Read-only file system", "./local/tmp/staging.json")
        )
        assert isinstance(err, LocalVolumeUnwritableError)
        assert err.path == "./local/tmp/staging.json"

    def test_enospc_is_left_for_the_disk_full_path(self) -> None:
        # ENOSPC stays disk_full_guard's job — this classifier must not touch it.
        assert (
            classify_unwritable_oserror(
                OSError(errno.ENOSPC, "No space left on device", "artifacts")
            )
            is None
        )

    def test_a_non_oserror_is_ignored(self) -> None:
        assert classify_unwritable_oserror(ValueError("not an OSError")) is None


class TestWriteProvenanceIsRequired:
    """A matching errno alone must not be read as a statement about the volume.

    Each case below carries an errno this classifier owns, and each must return
    `None`: the error says nothing about the app's local volume, so typing it
    would route a `PLATFORM`/`RESOURCE_EXHAUSTED` failure to whoever owns volumes
    and close it. Unclassified is the better outcome — it gets human eyes.
    """

    def test_permission_denied_reading_an_unrelated_file_is_not_typed(self) -> None:
        assert (
            classify_unwritable_oserror(
                OSError(errno.EACCES, "Permission denied", "/etc/ssl/private/key.pem")
            )
            is None
        )

    def test_an_absolute_path_outside_the_roots_is_not_typed(self) -> None:
        assert (
            classify_unwritable_oserror(
                OSError(errno.EROFS, "Read-only file system", "/usr/lib/thing.so")
            )
            is None
        )

    def test_eacces_with_no_filename_is_not_typed(self) -> None:
        # A socket bind or a seccomp denial names no file at all; typing it would
        # assert a volume is unwritable on an exception that never mentioned one.
        assert (
            classify_unwritable_oserror(OSError(errno.EACCES, "Permission denied"))
            is None
        )

    def test_eperm_with_no_filename_is_not_typed(self) -> None:
        assert (
            classify_unwritable_oserror(
                PermissionError(errno.EPERM, "Operation not permitted")
            )
            is None
        )

    def test_an_int_fd_instead_of_a_path_is_not_typed(self) -> None:
        # OSError.filename is whatever the kernel reported; it is not always a path.
        exc = OSError(errno.EACCES, "Permission denied")
        exc.filename = 7
        assert classify_unwritable_oserror(exc) is None

    def test_a_credential_path_never_reaches_the_wire_field(self) -> None:
        # `path` travels on the Temporal failure wire, so the gate doubles as the
        # reason an arbitrary filesystem path cannot be put there.
        secret = "/var/run/secrets/atlan/token"
        assert (
            classify_unwritable_oserror(OSError(errno.EACCES, "Denied", secret)) is None
        )

    def test_a_sibling_directory_sharing_the_prefix_is_not_typed(self) -> None:
        # "artifacts-backup" must not match the "artifacts" root by string prefix.
        assert (
            classify_unwritable_oserror(
                OSError(errno.EROFS, "Read-only file system", "artifacts-backup/x.json")
            )
            is None
        )

    def test_the_root_is_resolved_per_call_not_at_import(self, tmp_path: Path) -> None:
        # TEMPORARY_PATH is read from the environment, so a module-level constant
        # would freeze whichever value was live at first import.
        target = tmp_path / "scratch" / "out.json"
        exc = OSError(errno.EROFS, "Read-only file system", str(target))
        assert classify_unwritable_oserror(exc) is None
        with mock.patch(
            "application_sdk.common.atomic.TEMPORARY_PATH", str(tmp_path / "scratch")
        ):
            assert isinstance(
                classify_unwritable_oserror(exc), LocalVolumeUnwritableError
            )


class _WriteIn(Input, allow_unbounded_fields=True):
    x: str = ""


class _WriteOut(Output, allow_unbounded_fields=True):
    y: str = ""


class TestActivityBoundaryTranslatesIt:
    """The behaviour this change advertises is the *wiring*, so drive it.

    The classifier tests above all pass against a boundary that never calls it —
    a broken lazy import in `activities.py` would not fail one of them.
    """

    @pytest.mark.asyncio
    async def test_an_erofs_from_a_task_becomes_the_typed_leaf(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import TaskRegistry
        from application_sdk.app.task import task
        from application_sdk.execution._temporal import activities as activities_module
        from application_sdk.execution._temporal.activities import (
            TaskContext,
            create_activity_from_task,
        )
        from application_sdk.execution.errors import ApplicationError

        class _UnwritableApp(App):
            @task(timeout_seconds=60)
            async def write_artifact(self, input: _WriteIn) -> _WriteOut:
                raise OSError(
                    errno.EROFS,
                    "Read-only file system",
                    os.path.join("artifacts", "apps", "out.json"),
                )

            async def run(self, input: _WriteIn) -> _WriteOut:  # type: ignore[override]
                return await self.write_artifact(input)

        tasks = TaskRegistry.get_instance().get_tasks_for_app("_unwritable-app")
        activity_fn = create_activity_from_task(
            next(t for t in tasks if t.name == "write_artifact")
        )
        ctx = TaskContext(
            app_name="_unwritable-app",
            task_name="write_artifact",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-unwritable"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _WriteIn(x=""))

        assert exc_info.value.type == "LocalVolumeUnwritableError"

    @pytest.mark.asyncio
    async def test_an_unrelated_eacces_stays_untranslated(self) -> None:
        # The other half of the wiring: a non-provenanced OSError must reach the
        # caller as itself. Note it is not even wrapped in ApplicationError —
        # the boundary bare-`raise`s once the classifier declines, so this
        # asserts the original exception object survives, type and all.
        from application_sdk.app.base import App
        from application_sdk.app.registry import TaskRegistry
        from application_sdk.app.task import task
        from application_sdk.execution._temporal import activities as activities_module
        from application_sdk.execution._temporal.activities import (
            TaskContext,
            create_activity_from_task,
        )

        class _UnrelatedDenialApp(App):
            @task(timeout_seconds=60)
            async def read_secret(self, input: _WriteIn) -> _WriteOut:
                raise PermissionError(
                    errno.EACCES, "Permission denied", "/etc/ssl/private/key.pem"
                )

            async def run(self, input: _WriteIn) -> _WriteOut:  # type: ignore[override]
                return await self.read_secret(input)

        tasks = TaskRegistry.get_instance().get_tasks_for_app("_unrelated-denial-app")
        activity_fn = create_activity_from_task(
            next(t for t in tasks if t.name == "read_secret")
        )
        ctx = TaskContext(
            app_name="_unrelated-denial-app",
            task_name="read_secret",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-denial"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(PermissionError) as exc_info,
        ):
            await activity_fn(ctx, _WriteIn(x=""))

        assert exc_info.value.errno == errno.EACCES
        assert not isinstance(exc_info.value, LocalVolumeUnwritableError)

"""The activity boundary types a read-only / permission-denied local-write OSError.

`atomic.py`'s `disk_full_guard` deliberately stays narrow to ENOSPC/EDQUOT (see
`test_an_unrelated_oserror_is_not_reclassified`). The artifact-filesystem failure
family — `[Errno 30] Read-only file system: 'artifacts'` and siblings — is instead
typed at the activity boundary via `classify_unwritable_oserror`, so no write-path
invariant changes. These tests pin that classifier.
"""

from __future__ import annotations

import errno

from application_sdk.common.atomic import classify_unwritable_oserror
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

    def test_enospc_is_left_for_the_disk_full_path(self) -> None:
        # ENOSPC stays disk_full_guard's job — this classifier must not touch it.
        assert (
            classify_unwritable_oserror(
                OSError(errno.ENOSPC, "No space left on device")
            )
            is None
        )

    def test_a_non_oserror_is_ignored(self) -> None:
        assert classify_unwritable_oserror(ValueError("not an OSError")) is None

"""FND-318: a partial artifact must never be reachable at its final name.

The bug these cover is not "a write failed" — writes fail all the time and the
run notices. It is a write that fails and *leaves the wreckage where a correct
artifact goes*: a truncated file at the artifact's real name is carried
forward, uploaded, and integrity-checked against its own truncated bytes, and
the failure only surfaces much later in a consuming app's parser.

So every test here asserts the same two things about an induced ``ENOSPC``:
nothing is left at the final path, and the failure arrives typed. They are run
through the real call paths — ``copy_directory_parallel``,
``persist_marker_to_storage``, the writers — rather than against the helper
alone, because the helper being correct is not the claim; the claim is that the
paths apps actually use go through it.

``ENOSPC`` is induced at ``os.fsync`` for the atomic writers. That is not a
convenience: on a delayed-allocation filesystem it is *where a real ENOSPC
arrives*, since the ``write`` calls only dirty page cache. It is also the point
that makes the whole thing work — without an fsync before the rename, a short
write publishes silently and nothing raises anywhere.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.common._listing import safe_list_directory
from application_sdk.common.atomic import (
    PARTIAL_DIRNAME,
    atomic_copy,
    atomic_path,
    atomic_write,
    disk_full_guard,
    ensure_free_space,
)
from application_sdk.errors import DiskFullError, ResourceExhaustedError


def _enospc(*_args: Any, **_kwargs: Any) -> Any:
    """Stand-in for a filesystem with no room left."""
    raise OSError(errno.ENOSPC, "No space left on device")


class _Usage:
    """Minimal stand-in for ``shutil.disk_usage``'s named tuple."""

    def __init__(self, *, free: int) -> None:
        self.total = free * 2
        self.used = free
        self.free = free


def _names(directory: Path) -> set[str]:
    """Every entry directly under *directory*, staging directory included.

    Deliberately ``os.listdir`` rather than ``safe_list_directory``: these
    assertions are about what is *really* on disk, and the listing helper is
    one of the things under test.
    """
    return set(os.listdir(directory))


def _staged_files(directory: Path) -> list[Path]:
    """Files left behind in *directory*'s staging tree."""
    staging = directory / PARTIAL_DIRNAME
    return list(staging.iterdir()) if staging.exists() else []


# ---------------------------------------------------------------------------
# The guarantee, at the helper
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_the_final_path_does_not_exist_until_the_write_completes(
        self, tmp_path: Path
    ) -> None:
        artifact = tmp_path / "artifact.json"

        with atomic_write(artifact, operation="test write") as handle:
            handle.write(b'{"partial')
            assert (
                not artifact.exists()
            ), "the artifact was nameable while it was still being written"

        assert artifact.read_bytes() == b'{"partial'

    def test_enospc_leaves_no_file_at_the_final_path(self, tmp_path: Path) -> None:
        artifact = tmp_path / "artifact.json"

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError):
                with atomic_write(artifact, operation="test write") as handle:
                    handle.write(b"x" * 184)

        assert not artifact.exists()
        assert _staged_files(tmp_path) == [], "a staging file survived the failure"

    def test_enospc_does_not_disturb_an_artifact_already_in_place(
        self, tmp_path: Path
    ) -> None:
        """The rename is the only thing that ever touches the final path.

        A direct-to-final write truncates the previous artifact the moment it
        opens the file, so a failed re-write destroys a good file that was
        already there. Staging means the worst case is the run failing with the
        old artifact intact.
        """
        artifact = tmp_path / "artifact.json"
        artifact.write_bytes(b'{"complete": true}')

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError):
                with atomic_write(artifact, operation="test write") as handle:
                    handle.write(b'{"replacement')

        assert artifact.read_bytes() == b'{"complete": true}'

    def test_an_unrelated_oserror_is_not_reclassified(self, tmp_path: Path) -> None:
        """Only ENOSPC/EDQUOT become DiskFullError; this is a classifier, not a wrapper."""
        artifact = tmp_path / "artifact.json"

        def _eacces(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError(errno.EACCES, "Permission denied")

        with patch("os.fsync", _eacces):
            with pytest.raises(OSError) as caught:
                with atomic_write(artifact, operation="test write") as handle:
                    handle.write(b"x")

        assert not isinstance(caught.value, DiskFullError)
        assert caught.value.errno == errno.EACCES
        assert not artifact.exists()

    def test_a_failure_in_the_caller_publishes_nothing(self, tmp_path: Path) -> None:
        """Any exception, not just a disk one, leaves the final path untouched."""
        artifact = tmp_path / "artifact.json"

        with pytest.raises(ValueError):
            with atomic_write(artifact, operation="test write") as handle:
                handle.write(b"half a record")
                raise ValueError("serialisation blew up")

        assert not artifact.exists()
        assert _staged_files(tmp_path) == []

    def test_append_modes_are_rejected(self, tmp_path: Path) -> None:
        """An append through here would silently discard the existing artifact."""
        from application_sdk.common.errors import AtomicWriteModeError  # noqa: PLC0415

        with pytest.raises(AtomicWriteModeError, match="does not support mode"):
            with atomic_write(tmp_path / "a.json", operation="t", mode="ab"):
                pass

    def test_exclusive_create_modes_are_rejected(self, tmp_path: Path) -> None:
        """Publication is os.replace, which overwrites, so "x" would lie about
        refusing to clobber — the honest contract is last-writer-wins."""
        from application_sdk.common.errors import AtomicWriteModeError  # noqa: PLC0415

        with pytest.raises(AtomicWriteModeError, match="does not support mode"):
            with atomic_write(tmp_path / "a.json", operation="t", mode="xb"):
                pass

    def test_a_non_writing_mode_is_rejected(self, tmp_path: Path) -> None:
        from application_sdk.common.errors import AtomicWriteModeError  # noqa: PLC0415

        with pytest.raises(AtomicWriteModeError, match="needs a writing mode"):
            with atomic_write(tmp_path / "a.json", operation="t", mode="rb"):
                pass

    def test_text_mode_round_trips(self, tmp_path: Path) -> None:
        artifact = tmp_path / "marker.txt"

        with atomic_write(
            artifact, operation="test write", mode="w", encoding="utf-8"
        ) as handle:
            handle.write("2026-08-13T00:00:00Z")

        assert artifact.read_text(encoding="utf-8") == "2026-08-13T00:00:00Z"

    def test_concurrent_writers_of_the_same_artifact_do_not_share_a_staging_path(
        self, tmp_path: Path
    ) -> None:
        """A cancelled attempt's orphan and its retry must not collide."""
        artifact = tmp_path / "chunk-0.parquet"
        seen: list[Path] = []

        for _ in range(2):
            with atomic_path(artifact, operation="test write") as staging:
                seen.append(staging)
                staging.write_bytes(b"data")

        assert seen[0] != seen[1]
        assert all(path.parent.name == PARTIAL_DIRNAME for path in seen)

    def test_a_block_that_writes_nothing_is_an_error_not_a_silent_success(
        self, tmp_path: Path
    ) -> None:
        artifact = tmp_path / "artifact.parquet"

        with pytest.raises(FileNotFoundError):
            with atomic_path(artifact, operation="test write"):
                pass

        assert not artifact.exists()

    def test_a_staging_directory_that_cannot_be_created_fails_typed(
        self, tmp_path: Path
    ) -> None:
        """Creating .sdk-partial is itself a write; on a full filesystem its
        ENOSPC must arrive as DiskFullError, not escape as a bare OSError."""
        artifact = tmp_path / "artifact.json"

        with patch("application_sdk.common.atomic.os.makedirs", _enospc):
            with pytest.raises(DiskFullError) as caught:
                with atomic_write(artifact, operation="test write") as handle:
                    handle.write(b"x")

        assert caught.value.operation == "test write"
        assert not artifact.exists()


class TestAtomicCopy:
    def test_a_copy_that_runs_out_of_space_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "src.json"
        source.write_bytes(b'{"rows": 4000}')
        destination = tmp_path / "dest" / "src.json"
        destination.parent.mkdir()

        with patch("application_sdk.common.atomic.shutil.copy2", _enospc):
            with pytest.raises(DiskFullError):
                atomic_copy(source, destination, operation="test copy")

        assert not destination.exists()
        assert _staged_files(destination.parent) == []

    def test_a_successful_copy_publishes_the_whole_file(self, tmp_path: Path) -> None:
        source = tmp_path / "src.json"
        source.write_bytes(b'{"rows": 4000}')
        destination = tmp_path / "dest" / "src.json"
        destination.parent.mkdir()

        atomic_copy(source, destination, operation="test copy")

        assert destination.read_bytes() == source.read_bytes()


# ---------------------------------------------------------------------------
# The typed failure
# ---------------------------------------------------------------------------


class TestDiskFullError:
    def test_it_names_the_path_and_the_step(self, tmp_path: Path) -> None:
        artifact = tmp_path / "column-ancestral-201.json"

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError) as caught:
                with atomic_write(artifact, operation="carry-forward copy") as handle:
                    handle.write(b"x")

        error = caught.value
        assert error.path == str(artifact)
        assert error.operation == "carry-forward copy"
        assert error.free_bytes is not None
        assert "carry-forward copy" in str(error)
        assert str(artifact) in str(error)

    def test_it_is_a_resource_exhausted_leaf(self) -> None:
        """So RESOURCE_EXHAUSTED consumers route it without knowing the subtype."""
        error = DiskFullError(message="out of room")
        assert isinstance(error, ResourceExhaustedError)
        assert error.qualified_code == "RESOURCE_EXHAUSTED.RESOURCE_EXHAUSTED_DISK_FULL"
        assert error.effective_retryable is True

    def test_the_wire_envelope_carries_the_operator_signal(self) -> None:
        """It is the only thing that tells an operator to raise ephemeral storage."""
        error = DiskFullError(
            message="out of room",
            path="/local/tmp/x.json",
            operation="carry-forward copy",
            required_bytes=4096,
            free_bytes=10,
        )
        details = error.to_failure_details()

        assert details.evidence["path"] == "/local/tmp/x.json"
        assert details.evidence["required_bytes"] == 4096
        assert details.evidence["free_bytes"] == 10
        assert details.audience.value == "PLATFORM"
        assert details.suggested_action is not None
        assert "ephemeral-storage" in details.suggested_action

    def test_edquot_is_the_same_failure(self, tmp_path: Path) -> None:
        """A quota ceiling needs the same response as an empty volume."""
        quota_errno = getattr(errno, "EDQUOT", None)
        if quota_errno is None:
            pytest.skip("EDQUOT is not defined on this platform")

        def _over_quota(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError(quota_errno, "Disc quota exceeded")

        with patch("os.fsync", _over_quota):
            with pytest.raises(DiskFullError):
                with atomic_write(tmp_path / "a.json", operation="t") as handle:
                    handle.write(b"x")


class TestDiskFullGuard:
    def test_it_types_a_write_it_cannot_make_atomic(self, tmp_path: Path) -> None:
        with pytest.raises(DiskFullError) as caught:
            with disk_full_guard(
                tmp_path / "chunk-0.json", operation="json chunk write"
            ):
                _enospc()

        assert caught.value.operation == "json chunk write"

    def test_it_passes_every_other_oserror_through(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            with disk_full_guard(tmp_path / "x", operation="t"):
                raise FileNotFoundError(errno.ENOENT, "nope")


class TestEnsureFreeSpace:
    def test_a_plainly_impossible_write_fails_before_it_starts(
        self, tmp_path: Path
    ) -> None:
        with patch(
            "application_sdk.common.atomic.shutil.disk_usage",
            return_value=_Usage(free=1024),
        ):
            with pytest.raises(DiskFullError) as caught:
                ensure_free_space(tmp_path, 8 * 1024**3, operation="carry-forward copy")

        error = caught.value
        assert error.required_bytes == 8 * 1024**3
        assert error.free_bytes == 1024
        assert "8.0 GiB" in str(error)
        assert "1.0 KiB" in str(error)

    def test_a_write_that_fits_is_not_blocked(self, tmp_path: Path) -> None:
        ensure_free_space(tmp_path, 16, operation="t")  # should not raise

    def test_zero_is_a_no_op(self, tmp_path: Path) -> None:
        ensure_free_space(tmp_path, 0, operation="t")  # should not raise

    def test_an_unprobeable_filesystem_does_not_block_the_write(
        self, tmp_path: Path
    ) -> None:
        """The write's own failure stays the signal; the probe is an optimisation."""
        with patch("application_sdk.common.atomic.shutil.disk_usage", _enospc):
            ensure_free_space(tmp_path, 8 * 1024**3, operation="t")  # should not raise


# ---------------------------------------------------------------------------
# Staging must be invisible to everything that enumerates artifacts
# ---------------------------------------------------------------------------


class TestStagingIsInvisible:
    def test_a_directory_listing_does_not_see_an_in_flight_write(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "done.json").write_bytes(b"{}")

        with atomic_write(tmp_path / "in-flight.json", operation="t") as handle:
            handle.write(b"{")
            listed = safe_list_directory(tmp_path)

        assert [path.name for path in listed] == ["done.json"]

    def test_a_listing_still_works_when_pointed_at_the_staging_directory(
        self, tmp_path: Path
    ) -> None:
        """The exclusion is on descent, so an explicit target is not swallowed."""
        with atomic_write(tmp_path / "a.json", operation="t") as handle:
            handle.write(b"{")
            staged = safe_list_directory(tmp_path / PARTIAL_DIRNAME)

        assert len(staged) == 1

    async def test_a_prefix_upload_does_not_ship_an_in_flight_write(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.storage.batch import upload_prefix  # noqa: PLC0415
        from application_sdk.storage.factory import create_memory_store  # noqa: PLC0415

        (tmp_path / "done.json").write_bytes(b"{}")
        store = create_memory_store()

        with atomic_write(tmp_path / "in-flight.json", operation="t") as handle:
            handle.write(b"{")
            keys = await upload_prefix(tmp_path, "out", store, normalize=False)

        assert keys == ["out/done.json"]

    def test_the_staging_directory_is_left_behind_but_empty(
        self, tmp_path: Path
    ) -> None:
        """Removing it would race a concurrent writer; leaving it costs nothing."""
        with atomic_write(tmp_path / "a.json", operation="t") as handle:
            handle.write(b"{}")

        assert _names(tmp_path) == {"a.json", PARTIAL_DIRNAME}
        assert _staged_files(tmp_path) == []
        assert safe_list_directory(tmp_path) == [tmp_path / "a.json"]

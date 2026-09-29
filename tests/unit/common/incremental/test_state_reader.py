"""Tests for current state reader utilities.

Tests cover public functions with real business logic:
- download_current_state: First-run handling, S3 download, JSON counting,
  offloading the stale-state removal off the event loop, and a stale tree
  that cannot be removed in place.
"""

import errno
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental.state.state_reader import (
    _discard_local_state,
    download_current_state,
)
from application_sdk.storage.ops import _put


class TestDownloadCurrentState:
    """Tests for download_current_state (S3 download with first-run handling)."""

    async def test_first_run_returns_not_exists(self):
        """First run (S3 raises exception) returns exists=False."""
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            mock_store.side_effect = FileNotFoundError("not found")
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                _, _, exists, json_count = await download_current_state(
                    connection_qualified_name="t/c/123",
                    application_name="oracle",
                )

        assert exists is False
        assert json_count == 0

    async def test_existing_state_returns_exists(self):
        """Existing state with JSON files returns exists=True and file count."""
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                # Simulate S3 download creating JSON files
                async def fake_download(*args, **kwargs):
                    table_dir = state_dir / "table"
                    table_dir.mkdir(parents=True, exist_ok=True)
                    (table_dir / "chunk-0.json").write_text("{}")
                    (table_dir / "chunk-1.json").write_text("{}")

                mock_store.side_effect = fake_download

                dir_result, prefix, exists, json_count = await download_current_state(
                    connection_qualified_name="t/c/123",
                    application_name="oracle",
                )

        assert exists is True
        assert json_count == 2

    async def test_state_lands_unnested_under_current_state_dir(
        self, memory_store, tmp_path, monkeypatch
    ) -> None:
        """Downloaded state must land at ``<current-state>/<entity>/``.

        Real store instead of a mocked download, because the regression this
        guards (FND-340) was purely about *where* the bytes landed: the JSON
        count is taken with ``rglob``, so a nested tree still reported
        ``current_state_available=True`` while no keyed reader could use it.
        """
        from application_sdk.common.incremental import helpers

        monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path))
        prefix = "persistent-artifacts/apps/oracle/connection/123/current-state"
        await _put(
            f"{prefix}/table/chunk-0.json", b'{"t": 1}', memory_store, normalize=False
        )
        await _put(
            f"{prefix}/column/chunk-0.json", b'{"c": 1}', memory_store, normalize=False
        )

        state_dir, s3_prefix, exists, json_count = await download_current_state(
            connection_qualified_name="default/oracle/123",
            application_name="oracle",
        )

        assert s3_prefix == prefix
        assert exists is True
        assert json_count == 2
        assert (state_dir / "table" / "chunk-0.json").read_bytes() == b'{"t": 1}'
        assert (state_dir / "column" / "chunk-0.json").read_bytes() == b'{"c": 1}'
        assert not (state_dir / "persistent-artifacts").exists()

    async def test_empty_download_returns_not_exists(self):
        """Download that results in zero JSON files returns exists=False."""
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix"
            ),
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                _, _, exists, json_count = await download_current_state(
                    connection_qualified_name="t/c/123",
                    application_name="oracle",
                )

        assert exists is False
        assert json_count == 0

    async def test_clears_stale_directory(self):
        """Clears existing stale directory before downloading."""
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix"
            ),
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                # Create stale file
                (state_dir / "stale.json").write_text("{}")
                mock_path.return_value = state_dir

                _, _, exists, json_count = await download_current_state(
                    connection_qualified_name="t/c/123",
                    application_name="oracle",
                )

        # Stale file should be removed (directory was cleared)
        assert json_count == 0

    async def test_stale_directory_removal_offloaded_to_thread(self):
        """The stale-state rmtree must not run inline on the event loop.

        A prior run's current-state is one JSON file per asset, so the tree
        scales with the connection; removing it inline stalls every other
        coroutine — including a @task's auto-heartbeat — for the full duration.
        """
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix"
            ),
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
            patch(
                "application_sdk.common.incremental.state.state_reader.run_in_thread",
                new_callable=AsyncMock,
                side_effect=lambda func, *a, **kw: func(*a, **kw),
            ) as mock_offload,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                (state_dir / "stale.json").write_text("{}")
                mock_path.return_value = state_dir

                await download_current_state(
                    connection_qualified_name="t/c/123",
                    application_name="oracle",
                )

                assert (
                    mock_offload.await_args_list
                ), "stale-state removal was not offloaded"
                offloaded = mock_offload.await_args_list[0].args
                assert offloaded[0] is _discard_local_state
                assert offloaded[1] == state_dir


def _rmtree_blocked_by_live_writer(err: int):
    """shutil.rmtree as it behaves while a timed-out attempt's download
    threads still hold files open in the tree: the removal fails partway with
    ENOTEMPTY or EBUSY. With ignore_errors it swallows that, as the real one
    does."""

    def rmtree(path, ignore_errors=False, **kwargs):
        if not ignore_errors:
            raise OSError(err, os.strerror(err), str(path))

    return rmtree


@pytest.mark.parametrize("err", [errno.ENOTEMPTY, errno.EBUSY])
async def test_a_stale_tree_that_cannot_be_removed_does_not_fail_the_read(err):
    """A retry must not fail because the previous attempt's tree is still busy.

    The previous attempt timed out, but its download threads cannot be
    cancelled and keep writing into the tree, so removing it in place fails.
    The read has to start from an empty directory anyway, and never see the
    stale files."""
    seen_before_download: list[list[str]] = []

    async def fake_download(prefix, local_dir, strip_prefix):
        seen_before_download.append(sorted(p.name for p in Path(local_dir).iterdir()))
        (Path(local_dir) / "table").mkdir()
        (Path(local_dir) / "table" / "t.json").write_text("{}")

    with tempfile.TemporaryDirectory() as temp_dir:
        state_dir = Path(temp_dir) / "current-state"
        (state_dir / "column").mkdir(parents=True)
        (state_dir / "column" / "stale.json").write_text("{}")
        # Patched only around the read: the temp dir's own cleanup needs the
        # real rmtree.
        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.download_prefix",
                side_effect=fake_download,
            ),
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path",
                return_value=state_dir,
            ),
            patch(
                "application_sdk.common.incremental.state.state_reader.shutil.rmtree",
                side_effect=_rmtree_blocked_by_live_writer(err),
            ),
        ):
            returned, _, exists, json_count = await download_current_state(
                connection_qualified_name="t/c/123",
                application_name="oracle",
            )

        assert returned == state_dir
        assert seen_before_download == [[]]
        assert (exists, json_count) == (True, 1)
        assert not (state_dir / "column").exists()

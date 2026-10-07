"""Failure, cancellation and mirror contracts of the storage prefix primitives.

``download_prefix``, ``upload_prefix``, ``delete_prefix`` (per-key fallback)
and ``_gather_with_semaphore`` all fan out through ``_run_bounded``. The drain
tests pin the property ``asyncio.gather`` lacked: once the error or the
cancellation reaches the caller, no ``run_in_thread`` work the fan-out started
is still running.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk._runtime import offload as offload_module
from application_sdk._runtime.offload import (
    drain_offloads,
    run_in_thread,
    tracking_offloads,
)
from application_sdk._runtime.progress import ProgressTracker, bind_progress_tracker
from application_sdk.common._listing import INTERNAL_DIRNAMES, SYNC_INDEX_DIRNAME
from application_sdk.storage import batch as batch_module
from application_sdk.storage._concurrency import _gather_with_semaphore, _run_bounded
from application_sdk.storage.batch import download_prefix, list_keys, upload_prefix
from application_sdk.storage.errors import StorageConfigError, StorageError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes, _put

#: How long the offloaded "slow" call blocks its thread. Long enough that an
#: undrained fan-out returns well before it ends; short enough to keep the
#: suite fast.
_THREAD_SECONDS = 0.3

#: How long a test waits for an offloaded call to start before failing. The unit
#: job has no per-test timeout, so every wait a regression could make endless is
#: bounded by this instead.
_START_TIMEOUT = 5.0


async def _wait_started(event: threading.Event) -> None:
    """Wait for an offloaded call's start event; fail instead of hanging.

    The timeout goes to ``Event.wait`` itself, so a call that never starts
    leaves no helper thread blocked behind the failed test.
    """
    if not await asyncio.to_thread(event.wait, _START_TIMEOUT):
        raise AssertionError("offloaded call did not start")


@pytest.fixture
def store():
    return create_memory_store()


class _BlockingCall:
    """A blocking callable for ``run_in_thread`` that records its lifecycle."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        time.sleep(_THREAD_SECONDS)
        self.finished.set()

    async def wait_started(self) -> None:
        await _wait_started(self.started)


async def _fail_once_started(call: _BlockingCall) -> None:
    await call.wait_started()
    raise StorageError("boom")


async def _cancel_once_started(
    call: _BlockingCall, run: Callable[[], Awaitable[object]]
) -> None:
    """Start *run*, cancel it once *call*'s thread is running, await the unwind."""
    task = asyncio.create_task(run())
    await call.wait_started()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# _run_bounded / _gather_with_semaphore
# ---------------------------------------------------------------------------


class TestRunBounded:
    async def test_failure_drains_in_flight_threads(self) -> None:
        call = _BlockingCall()

        with pytest.raises(StorageError):
            await _run_bounded([run_in_thread(call), _fail_once_started(call)], 4)

        assert call.finished.is_set(), "sibling thread still running after raise"

    async def test_cancellation_drains_in_flight_threads(self) -> None:
        call = _BlockingCall()

        await _cancel_once_started(call, lambda: _run_bounded([run_in_thread(call)], 4))

        assert call.finished.is_set(), "thread still running after cancel"

    async def test_error_is_a_bare_storage_error(self) -> None:
        async def fail() -> None:
            raise StorageError("boom")

        with pytest.raises(StorageError) as exc_info:
            await _run_bounded([fail(), fail()], 2)

        assert not isinstance(exc_info.value, BaseExceptionGroup)
        assert isinstance(exc_info.value.__context__, BaseExceptionGroup)

    async def test_the_storage_error_keeps_its_own_cause(self) -> None:
        """Unwrapping the group must not replace the leaf's cause with the group.

        A caller walking ``__cause__`` for the filesystem error behind a failed
        download (a lost staging directory, a full disk) has to find it.
        """
        root = FileNotFoundError("staging directory vanished")

        async def fail() -> None:
            raise StorageError("Failed to write downloaded file") from root

        with pytest.raises(StorageError) as exc_info:
            await _run_bounded([fail()], 2)

        assert exc_info.value.__cause__ is root

    async def test_non_storage_failure_surfaces_as_the_group(self) -> None:
        async def fail() -> None:
            raise ValueError("not a storage failure")

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await _run_bounded([fail()], 2)

        assert [type(e) for e in exc_info.value.exceptions] == [ValueError]

    async def test_results_keep_input_order_and_respect_the_bound(self) -> None:
        running = 0
        peak = 0

        async def item(i: int) -> int:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01 * (5 - i))
            running -= 1
            return i

        assert await _run_bounded([item(i) for i in range(5)], 2) == list(range(5))
        assert peak == 2

    async def test_gather_with_semaphore_drains_on_failure(self) -> None:
        call = _BlockingCall()

        with pytest.raises(StorageError):
            await _gather_with_semaphore(
                [run_in_thread(call), _fail_once_started(call)], asyncio.Semaphore(4)
            )

        assert call.finished.is_set()

    async def test_offloads_outside_a_fanout_are_untracked(self) -> None:
        """A plain ``run_in_thread`` keeps its old behaviour: no scope, no marker."""
        assert await run_in_thread(lambda: 7) == 7


# ---------------------------------------------------------------------------
# The drain waits only while its threads are progressing
# ---------------------------------------------------------------------------

#: The no-progress allowance these tests bind. Small, so a stalled drain gives
#: up quickly; the progressing case below spans several of it.
_BUDGET = 0.15

#: A wedged call is released after this, so on a drain that never gives up the
#: test fails on elapsed time instead of hanging the suite.
_SAFETY_RELEASE = 3.0


async def _until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0.005)


def _wedged_volume_fsync(release: threading.Event) -> None:
    """Stands in for an fsync on a wedged volume: blocks until released."""
    release.wait()


class TestDrainGivesUpWithoutProgress:
    async def test_a_wedged_thread_does_not_hold_the_failure(self) -> None:
        release = threading.Event()
        threading.Timer(_SAFETY_RELEASE, release.set).start()
        started = threading.Event()

        def wedged() -> None:
            started.set()
            _wedged_volume_fsync(release)

        async def fail_once_started() -> None:
            await _wait_started(started)
            raise StorageError("boom")

        began = time.monotonic()
        try:
            with (
                bind_progress_tracker(ProgressTracker(max_no_progress_seconds=_BUDGET)),
                patch.object(offload_module.logger, "warning") as warning,
                pytest.raises(StorageError, match="boom"),
            ):
                await _run_bounded([run_in_thread(wedged), fail_once_started()], 4)
            elapsed = time.monotonic() - began
        finally:
            release.set()

        assert elapsed < _SAFETY_RELEASE / 2, f"drain held the failure {elapsed:.2f}s"
        # The wedged call is also reported as stranded (FND-2973) when its
        # caller is cancelled; only the drain's own give-up line is asserted here.
        drain_reports = [
            c for c in warning.call_args_list if "Stopped waiting" in c.args[0]
        ]
        assert len(drain_reports) == 1
        assert "wedged" in drain_reports[0].args[-1], "stuck call not named"

    async def test_a_wedged_thread_does_not_swallow_the_cancellation(self) -> None:
        release = threading.Event()
        threading.Timer(_SAFETY_RELEASE, release.set).start()
        call = _BlockingCall()

        def wedged() -> None:
            call.started.set()
            _wedged_volume_fsync(release)

        began = time.monotonic()
        try:
            with bind_progress_tracker(
                ProgressTracker(max_no_progress_seconds=_BUDGET)
            ):
                await _cancel_once_started(
                    call, lambda: _run_bounded([run_in_thread(wedged)], 4)
                )
            elapsed = time.monotonic() - began
        finally:
            release.set()

        assert elapsed < _SAFETY_RELEASE / 2, f"cancel held {elapsed:.2f}s"

    async def test_a_progressing_drain_outlives_the_allowance(self) -> None:
        """Threads finishing one by one keep the drain waiting past the allowance.

        Driven by an injected clock and one release per thread, not by sleeps:
        each finish lands 0.6 allowances after the last, so the drain's stall
        clock never reaches the allowance, while the whole drain spans 2.4 of
        them. Real-time spacing made this flaky on loaded runners.
        """
        budget = 10.0
        now = [0.0]
        count = 4
        started = [threading.Event() for _ in range(count)]
        releases = [threading.Event() for _ in range(count)]
        finished: list[int] = []

        def staggered(i: int) -> None:
            started[i].set()
            releases[i].wait(_SAFETY_RELEASE)
            finished.append(i)

        with tracking_offloads() as pending:
            tasks = [
                asyncio.ensure_future(run_in_thread(staggered, i)) for i in range(count)
            ]
            # A start handshake, not a delay: a call still queued when its task
            # is cancelled would be cancelled by the drain and never finish.
            for event in started:
                await _wait_started(event)
            for task in tasks:
                task.cancel()
            drain = asyncio.ensure_future(
                drain_offloads(
                    pending, max_no_progress_seconds=budget, clock=lambda: now[0]
                )
            )
            for i in range(count):
                now[0] += budget * 0.6
                releases[i].set()
                await asyncio.wait_for(
                    _until(lambda: len(finished) > i), _START_TIMEOUT
                )
                await asyncio.sleep(0.01)  # let the drain observe the finish
                assert not drain.done() or i == count - 1, f"gave up after {i + 1}"
            await asyncio.wait_for(drain, _SAFETY_RELEASE)

        assert now[0] > 2 * budget
        assert sorted(finished) == list(range(count))
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_an_explicit_allowance_overrides_the_attempts(self) -> None:
        release = threading.Event()
        threading.Timer(_SAFETY_RELEASE, release.set).start()
        started = threading.Event()

        def wedged() -> None:
            started.set()
            _wedged_volume_fsync(release)

        with tracking_offloads() as pending:
            task = asyncio.ensure_future(run_in_thread(wedged))
            await _wait_started(started)
            task.cancel()
            began = time.monotonic()
            try:
                with bind_progress_tracker(
                    ProgressTracker(max_no_progress_seconds=_SAFETY_RELEASE * 10)
                ):
                    await drain_offloads(pending, max_no_progress_seconds=_BUDGET)
                elapsed = time.monotonic() - began
            finally:
                release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert elapsed < _SAFETY_RELEASE / 2


# ---------------------------------------------------------------------------
# Each primitive drains
# ---------------------------------------------------------------------------


class TestPrimitivesDrain:
    async def test_download_prefix_drains_on_failure(self, store, tmp_path) -> None:
        await _put("p/a.txt", b"a", store, normalize=False)
        await _put("p/b.txt", b"b", store, normalize=False)
        call = _BlockingCall()

        async def fake_download(key, *args, **kwargs):
            if key == "p/a.txt":
                await run_in_thread(call)
            else:
                await _fail_once_started(call)

        with (
            patch.object(batch_module, "download_file_chunked", fake_download),
            pytest.raises(StorageError),
        ):
            await download_prefix("p/", tmp_path, store, normalize=False)

        assert call.finished.is_set()

    async def test_download_prefix_drains_on_cancel(self, store, tmp_path) -> None:
        await _put("p/a.txt", b"a", store, normalize=False)
        call = _BlockingCall()

        async def fake_download(key, *args, **kwargs):
            await run_in_thread(call)

        with patch.object(batch_module, "download_file_chunked", fake_download):
            await _cancel_once_started(
                call,
                lambda: download_prefix("p/", tmp_path, store, normalize=False),
            )

        assert call.finished.is_set()

    async def test_upload_prefix_drains_on_failure(self, store, tmp_path) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        call = _BlockingCall()

        async def fake_upload(key, *args, **kwargs):
            if key.endswith("a.txt"):
                await run_in_thread(call)
            else:
                await _fail_once_started(call)

        with (
            patch.object(batch_module, "upload_file", fake_upload),
            pytest.raises(StorageError),
        ):
            await upload_prefix(tmp_path, "out", store, normalize=False)

        assert call.finished.is_set()

    async def test_upload_prefix_drains_on_cancel(self, store, tmp_path) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        call = _BlockingCall()

        async def fake_upload(key, *args, **kwargs):
            await run_in_thread(call)

        with patch.object(batch_module, "upload_file", fake_upload):
            await _cancel_once_started(
                call, lambda: upload_prefix(tmp_path, "out", store, normalize=False)
            )

        assert call.finished.is_set()

    async def test_delete_fallback_drains_on_failure(self, store) -> None:
        call = _BlockingCall()

        async def fake_delete(path, *args, **kwargs):
            if path == "k/a":
                await run_in_thread(call)
                return True
            await _fail_once_started(call)
            return True

        with (
            patch.object(batch_module, "_delete_object", fake_delete),
            pytest.raises(StorageError) as exc_info,
        ):
            await batch_module._delete_paths_individually(store, ["k/a", "k/b"])

        assert not isinstance(exc_info.value, BaseExceptionGroup)
        assert call.finished.is_set()


# ---------------------------------------------------------------------------
# download_prefix(sync=True)
# ---------------------------------------------------------------------------


def _downloaded_keys() -> tuple[list[str], Callable[..., Awaitable[None]]]:
    """A pass-through ``download_file_chunked`` that records each key fetched."""
    fetched: list[str] = []
    real = batch_module.download_file_chunked

    async def spy(key, *args, **kwargs):
        fetched.append(key)
        await real(key, *args, **kwargs)

    return fetched, spy


class TestDownloadPrefixSync:
    async def test_second_sync_skips_current_files(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await _put("s/sub/b.txt", b"beta", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            dests = await download_prefix(
                "s/", tmp_path, store, normalize=False, sync=True
            )

        assert fetched == []
        # The return value is the mirror's content, skipped files included.
        assert sorted(Path(d).name for d in dests) == ["a.txt", "b.txt"]

    async def test_changed_object_is_downloaded_again(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await _put("s/b.txt", b"beta", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        await _put("s/a.txt", b"ALPHA", store, normalize=False)  # new etag

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert fetched == ["s/a.txt"]
        assert (tmp_path / "s" / "a.txt").read_bytes() == b"ALPHA"

    async def test_local_size_drift_is_downloaded_again(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        (tmp_path / "s" / "a.txt").write_bytes(b"truncated-and-then-some")

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert fetched == ["s/a.txt"]
        assert (tmp_path / "s" / "a.txt").read_bytes() == b"alpha"

    async def test_same_size_local_replacement_is_downloaded_again(
        self, store, tmp_path
    ) -> None:
        """Different bytes of the same length: the recorded mtime is what catches it."""
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        local = tmp_path / "s" / "a.txt"
        before = local.stat().st_mtime_ns
        local.write_bytes(b"ALPHA")
        # Pin a distinct mtime: a coarse filesystem clock could otherwise land
        # the rewrite on the same tick as the download.
        os.utime(local, ns=(before + 1_000_000_000, before + 1_000_000_000))

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert fetched == ["s/a.txt"]
        assert local.read_bytes() == b"alpha"

    async def test_a_key_inside_the_sync_directory_is_refused(
        self, store, tmp_path
    ) -> None:
        """An object keyed where the index lives would be overwritten by it."""
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await _put(
            f"s/{SYNC_INDEX_DIRNAME}/index.jsonl", b"theirs", store, normalize=False
        )

        with pytest.raises(StorageConfigError, match="SDK working directory"):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        # Refused before anything was fetched or written.
        assert not (tmp_path / "s" / "a.txt").exists()

    async def test_without_sync_nothing_is_refused(self, store, tmp_path) -> None:
        await _put(f"s/{SYNC_INDEX_DIRNAME}/x.txt", b"theirs", store, normalize=False)

        await download_prefix("s/", tmp_path, store, normalize=False)

        assert (tmp_path / "s" / SYNC_INDEX_DIRNAME / "x.txt").read_bytes() == b"theirs"

    async def test_cancelled_prune_is_drained(self, store, tmp_path) -> None:
        """A prune thread that outlives a cancel would unlink a retry's output."""
        await _put("s/a.txt", b"alpha", store, normalize=False)
        call = _BlockingCall()

        def slow_prune(*_args, **_kwargs) -> None:
            call()

        with patch.object(batch_module, "_prune_unlisted", slow_prune):
            await _cancel_once_started(
                call,
                lambda: download_prefix(
                    "s/", tmp_path, store, normalize=False, sync=True
                ),
            )

        assert call.finished.is_set(), "prune thread still running after cancel"

    async def test_index_is_one_json_row_per_file(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await _put("s/sub/b.txt", b"beta", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        lines = batch_module._sync_index_path(tmp_path / "s").read_text().splitlines()

        assert json.loads(lines[0]) == {"version": batch_module._SYNC_INDEX_VERSION}
        assert sorted(json.loads(line)["path"] for line in lines[1:]) == [
            "a.txt",
            "sub/b.txt",
        ]

    async def test_a_version_1_index_means_one_full_download(
        self, store, tmp_path
    ) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        batch_module._sync_index_path(tmp_path / "s").write_text(
            json.dumps({"version": 1, "files": {"a.txt": {"size": 5, "etag": "x"}}})
        )

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert fetched == ["s/a.txt"]

    async def test_unlisted_local_files_are_pruned(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        stale = tmp_path / "s" / "old" / "gone.txt"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        partial = tmp_path / "s" / ".sdk-partial" / "x.part"
        partial.parent.mkdir()
        partial.write_bytes(b"in flight")

        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert not stale.exists()
        assert (tmp_path / "s" / "a.txt").exists()
        # SDK working directories are not the listing's business.
        assert partial.exists()

    async def test_prune_stays_inside_the_prefix_tree(self, store, tmp_path) -> None:
        """Without strip_prefix, a sibling prefix in the same local_dir survives."""
        await _put("s/a.txt", b"alpha", store, normalize=False)
        sibling = tmp_path / "other" / "keep.txt"
        sibling.parent.mkdir()
        sibling.write_bytes(b"not mine")

        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert sibling.exists()

    async def test_strip_prefix_syncs_local_dir_itself(self, store, tmp_path) -> None:
        await _put("run/state/a.txt", b"alpha", store, normalize=False)
        stale = tmp_path / "stale.txt"
        stale.write_bytes(b"x")

        await download_prefix(
            "run/state", tmp_path, store, normalize=False, strip_prefix=True, sync=True
        )

        assert (tmp_path / "a.txt").read_bytes() == b"alpha"
        assert not stale.exists()
        assert batch_module._sync_index_path(tmp_path).exists()

    async def test_suffix_limits_what_is_pruned(self, store, tmp_path) -> None:
        await _put("s/a.parquet", b"p", store, normalize=False)
        other = tmp_path / "s" / "notes.json"
        other.parent.mkdir()
        other.write_bytes(b"{}")
        stale = tmp_path / "s" / "old.parquet"
        stale.write_bytes(b"old")

        await download_prefix(
            "s/", tmp_path, store, normalize=False, suffix=".parquet", sync=True
        )

        assert other.exists()
        assert not stale.exists()

    async def test_failed_sync_does_not_vouch_for_the_old_etag(
        self, store, tmp_path
    ) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        await _put("s/a.txt", b"ALPHA", store, normalize=False)

        async def fail(*args, **kwargs):
            raise StorageError("boom")

        with (
            patch.object(batch_module, "download_file_chunked", fail),
            pytest.raises(StorageError),
        ):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        index = batch_module._read_sync_index(tmp_path / "s")
        assert "a.txt" not in index

    async def test_unreadable_index_means_full_download(self, store, tmp_path) -> None:
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix("s/", tmp_path, store, normalize=False, sync=True)
        batch_module._sync_index_path(tmp_path / "s").write_text("{not json")

        fetched, spy = _downloaded_keys()
        with patch.object(batch_module, "download_file_chunked", spy):
            await download_prefix("s/", tmp_path, store, normalize=False, sync=True)

        assert fetched == ["s/a.txt"]

    async def test_sync_index_is_not_uploaded(self, store, tmp_path) -> None:
        assert SYNC_INDEX_DIRNAME in INTERNAL_DIRNAMES
        await _put("s/a.txt", b"alpha", store, normalize=False)
        await download_prefix(
            "s/", tmp_path, store, normalize=False, strip_prefix=True, sync=True
        )

        keys = await upload_prefix(tmp_path, "back", store, normalize=False)

        assert keys == ["back/a.txt"]


# ---------------------------------------------------------------------------
# upload_prefix(prune=True)
# ---------------------------------------------------------------------------


class TestUploadPrefixPrune:
    async def test_deletes_only_keys_not_uploaded(self, store, tmp_path) -> None:
        await _put("out/old.txt", b"old", store, normalize=False)
        await _put("out/old.txt.sha256", b"0" * 64, store, normalize=False)
        await _put("out/keep.txt", b"previous", store, normalize=False)
        await _put("out_backup/x.txt", b"sibling", store, normalize=False)
        (tmp_path / "keep.txt").write_bytes(b"current")
        (tmp_path / "new.txt").write_bytes(b"new")

        uploaded = await upload_prefix(
            tmp_path, "out", store, normalize=False, prune=True
        )

        remaining = set(await list_keys("out/", store, normalize=False))
        assert set(uploaded) == {"out/keep.txt", "out/new.txt"}
        assert "out/old.txt" not in remaining
        assert "out/old.txt.sha256" not in remaining
        assert set(uploaded) <= remaining
        # Anything else left is an uploaded key's own sidecar.
        assert remaining - set(uploaded) <= {f"{k}.sha256" for k in uploaded}
        assert await _get_bytes("out/keep.txt", store, normalize=False) == b"current"
        assert await list_keys("out_backup/", store, normalize=False) == [
            "out_backup/x.txt"
        ]

    async def test_failed_upload_prunes_nothing(self, store, tmp_path) -> None:
        await _put("out/old.txt", b"old", store, normalize=False)
        (tmp_path / "a.txt").write_bytes(b"a")

        async def fail(*args, **kwargs):
            raise StorageError("boom")

        with (
            patch.object(batch_module, "upload_file", fail),
            pytest.raises(StorageError),
        ):
            await upload_prefix(tmp_path, "out", store, normalize=False, prune=True)

        assert await list_keys("out/", store, normalize=False) == ["out/old.txt"]

    async def test_empty_prefix_is_refused(self, store, tmp_path) -> None:
        await _put("anything.txt", b"x", store, normalize=False)
        (tmp_path / "a.txt").write_bytes(b"a")

        with pytest.raises(StorageConfigError):
            await upload_prefix(tmp_path, "", store, normalize=False, prune=True)

        assert await list_keys("", store, normalize=False) == ["anything.txt"]

    async def test_default_prunes_nothing(self, store, tmp_path) -> None:
        await _put("out/old.txt", b"old", store, normalize=False)
        (tmp_path / "a.txt").write_bytes(b"a")

        await upload_prefix(tmp_path, "out", store, normalize=False)

        assert "out/old.txt" in await list_keys("out/", store, normalize=False)

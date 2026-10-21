"""Same-pod FileReference hand-off: trust the marker's stat, hash only off the loop.

FND-4081. A persisted ref handed to the next task on the same pod used to be
re-hashed synchronously, on the event loop, up to three times — a 2 GiB ref
held the loop for seconds and stopped heartbeats. The local ``.sha256`` marker
now records ``(size, mtime_ns, inode)`` beside the digest; a matching stat is
trusted, anything else is re-hashed in a worker thread or re-downloaded.

The trust is what makes this fast, so the tests here pin what it must still
catch: a file whose content changed (mtime moves), whose length changed, or
that no longer has the size the producer recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import FileReference
from application_sdk.storage import integrity
from application_sdk.storage._local_marker import (
    file_fingerprint,
    format_marker,
    parse_marker,
    read_local_marker,
    trusted_digest,
)
from application_sdk.storage.chunked import _InOrderHasher
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import (
    has_refs_to_materialize,
    materialize_file_refs,
)
from application_sdk.storage.ops import download_file_chunked, upload_file
from application_sdk.storage.reference import (
    materialize_file_reference,
    persist_file_reference,
)
from tests.unit.loop_probe import LoopProbe

pytestmark = pytest.mark.asyncio

_PAYLOAD = b"0123456789abcdef" * 64  # 1 KiB


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _OneRef(Input):
    ref: FileReference


class _TwoRefs(Input):
    first: FileReference
    second: FileReference


async def _persisted(
    tmp_path: Path, data: bytes = _PAYLOAD
) -> tuple[Any, Path, FileReference]:
    """Persist *data* the way the interceptor does; return (store, file, durable ref)."""
    store = create_memory_store()
    local = tmp_path / "out.bin"
    local.write_bytes(data)
    ref = await persist_file_reference(store, FileReference.from_local(local))
    return store, local, ref


def _bump_mtime(path: Path) -> None:
    """Move mtime forward explicitly: same-tick rewrites can keep it on coarse clocks."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


class _NoHash:
    """Patch for the synchronous hash primitive that fails if it is ever called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, path: Path) -> str:
        self.calls += 1
        raise AssertionError(f"hashed {path}; a matching stat must be trusted")


# ---------------------------------------------------------------------------
# Marker format
# ---------------------------------------------------------------------------


class TestLocalMarker:
    async def test_round_trips_digest_and_fingerprint(self) -> None:
        digest = "ab" * 32
        marker = parse_marker(format_marker(digest, (7, 123, 456)))
        assert marker is not None
        assert (marker.sha256, marker.fingerprint) == (digest, (7, 123, 456))

    async def test_reads_a_digest_only_marker_without_trusting_it(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "f.bin"
        f.write_bytes(b"x")
        (tmp_path / "f.bin.sha256").write_text(_sha(b"x"))

        marker = read_local_marker(str(f))
        assert marker is not None and marker.fingerprint is None
        assert trusted_digest(str(f)) is None

    @pytest.mark.parametrize(
        "text", ["", "deadbeef", "a" * 64 + " 1 2", "a" * 64 + " 1 x 3", "g" * 64]
    )
    async def test_rejects_malformed_markers(self, text: str) -> None:
        assert parse_marker(text) is None

    async def test_a_symlink_is_never_trusted_on_stat(self, tmp_path: Path) -> None:
        target = tmp_path / "target.bin"
        target.write_bytes(b"x")
        link = tmp_path / "link.bin"
        link.symlink_to(target)
        assert file_fingerprint(str(link)) is None


# ---------------------------------------------------------------------------
# Same-pod hand-off
# ---------------------------------------------------------------------------


class TestTrustedHandOff:
    async def test_persist_records_size_and_a_trusted_marker(
        self, tmp_path: Path
    ) -> None:
        _, local, ref = await _persisted(tmp_path)

        assert ref.size_bytes == len(_PAYLOAD)
        assert trusted_digest(str(local), size_bytes=ref.size_bytes) == _sha(_PAYLOAD)

    async def test_a_matching_stat_is_reused_without_reading_the_file(
        self, tmp_path: Path
    ) -> None:
        store, _, ref = await _persisted(tmp_path)
        inp = _OneRef(ref=ref)
        no_hash = _NoHash()

        with patch.object(integrity, "_sha256_file_sync", no_hash):
            assert has_refs_to_materialize(inp) is False
            assert await materialize_file_refs(store, inp) is inp

        assert no_hash.calls == 0


class TestCorruptionIsStillCaught:
    """``Done when``: a corrupt file with a changed mtime or size is still caught."""

    async def test_same_size_rewrite_is_rehashed_and_redownloaded(
        self, tmp_path: Path
    ) -> None:
        store, local, ref = await _persisted(tmp_path)
        local.write_bytes(b"X" * len(_PAYLOAD))  # same length, different bytes
        _bump_mtime(local)

        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD

    async def test_truncated_file_is_redownloaded(self, tmp_path: Path) -> None:
        store, local, ref = await _persisted(tmp_path)
        with local.open("r+b") as fh:
            fh.truncate(10)

        assert has_refs_to_materialize(_OneRef(ref=ref)) is True
        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD

    async def test_size_disagreeing_with_size_bytes_is_redownloaded(
        self, tmp_path: Path
    ) -> None:
        """The ref's recorded size is checked even when the marker matches the file."""
        store, local, ref = await _persisted(tmp_path)
        from application_sdk.storage.reference import _write_local_sidecar

        local.write_bytes(_PAYLOAD + b"tail")
        # A marker re-stamped over the wrong bytes still disagrees with the ref.
        _write_local_sidecar(
            str(local), _sha(_PAYLOAD + b"tail"), file_fingerprint(str(local))
        )

        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD

    async def test_inode_change_is_not_trusted(self, tmp_path: Path) -> None:
        """``os.replace`` of a same-size file with a copied mtime moves the inode."""
        store, local, ref = await _persisted(tmp_path)
        st = local.stat()
        swap = tmp_path / "swap.bin"
        swap.write_bytes(b"Y" * len(_PAYLOAD))
        os.utime(swap, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(swap, local)

        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD


class TestUntrustedMarkerFallsBackOffTheLoop:
    async def test_digest_only_marker_is_hashed_off_loop_and_upgraded(
        self, tmp_path: Path
    ) -> None:
        store, local, ref = await _persisted(tmp_path)
        # What an SDK before FND-4081 left beside the file.
        (tmp_path / "out.bin.sha256").write_text(_sha(_PAYLOAD))
        inp = _OneRef(ref=ref)
        probe = LoopProbe()
        real = integrity._sha256_file_sync

        def _probed(path: Path) -> str:
            probe.block()
            return real(path)

        with patch.object(integrity, "_sha256_file_sync", _probed):
            # The cheap pre-check cannot tell, so it defers to the async pass.
            assert has_refs_to_materialize(inp) is True
            out = await probe.run(lambda: materialize_file_refs(store, inp))

        probe.assert_loop_stayed_free("materialize_file_refs fallback hash")
        assert out is inp  # verified in place: nothing downloaded
        assert trusted_digest(str(local)) == _sha(_PAYLOAD)  # re-stamped

    async def test_pre_check_never_hashes(self, tmp_path: Path) -> None:
        _, _, ref = await _persisted(tmp_path)
        (tmp_path / "out.bin.sha256").write_text(_sha(_PAYLOAD))
        no_hash = _NoHash()

        with patch.object(integrity, "_sha256_file_sync", no_hash):
            has_refs_to_materialize(_OneRef(ref=ref))

        assert no_hash.calls == 0

    async def test_refs_sharing_a_local_path_hash_it_once(self, tmp_path: Path) -> None:
        """The old path hashed a persisted ref up to three times per hand-off."""
        store, _, ref = await _persisted(tmp_path)
        (tmp_path / "out.bin.sha256").write_text(_sha(_PAYLOAD))
        twin = ref.model_copy()
        calls = 0
        real = integrity._sha256_file_sync

        def _counting(path: Path) -> str:
            nonlocal calls
            calls += 1
            return real(path)

        with patch.object(integrity, "_sha256_file_sync", _counting):
            await materialize_file_refs(store, _TwoRefs(first=ref, second=twin))

        assert calls == 1


class TestSizeBytesWireContract:
    """``size_bytes`` crosses Temporal payloads; see docs/standards/cross-repo-contracts.md."""

    async def test_a_ref_from_an_older_producer_decodes_with_no_size(self) -> None:
        ref = FileReference.model_validate(
            {"storage_path": "k/f.bin", "is_durable": True, "local_path": "/x"}
        )
        assert ref.size_bytes is None

    async def test_the_field_is_serialised_under_its_own_name(self) -> None:
        dumped = FileReference(storage_path="k", is_durable=True, size_bytes=5)
        assert dumped.model_dump(mode="json")["size_bytes"] == 5


class TestFingerprintIsOfTheHashedBytes:
    """A write that lands while the file is hashed must not inherit the digest.

    The marker's stat has to describe the bytes the digest was computed over.
    Stat-ing at marker-write time would pair the old digest with the new
    bytes' stat, and every later hand-off would trust the new bytes unread.
    """

    async def test_persist_leaves_the_marker_untrusted_if_the_file_moved(
        self, tmp_path: Path
    ) -> None:
        store = create_memory_store()
        local = tmp_path / "out.bin"
        local.write_bytes(_PAYLOAD)
        real_upload = upload_file

        async def _upload_then_rewrite(*args: Any, **kwargs: Any) -> str | None:
            digest = await real_upload(*args, **kwargs)
            local.write_bytes(b"Z" * len(_PAYLOAD))  # same size, after the hash
            _bump_mtime(local)
            return digest

        with patch("application_sdk.storage.ops.upload_file", _upload_then_rewrite):
            await persist_file_reference(store, FileReference.from_local(local))

        marker = read_local_marker(str(local))
        assert marker is not None and marker.sha256 == _sha(_PAYLOAD)
        assert trusted_digest(str(local)) is None

    async def test_rehash_does_not_restamp_a_file_that_moved_during_the_hash(
        self, tmp_path: Path
    ) -> None:
        store, local, ref = await _persisted(tmp_path)
        (tmp_path / "out.bin.sha256").write_text(_sha(_PAYLOAD))  # untrusted
        real = integrity._sha256_file_sync

        def _hash_then_rewrite(path: Path) -> str:
            digest = real(path)
            local.write_bytes(b"Z" * len(_PAYLOAD))
            _bump_mtime(local)
            return digest

        with patch.object(integrity, "_sha256_file_sync", _hash_then_rewrite):
            out = await materialize_file_refs(store, _OneRef(ref=ref))

        # Not reused on the strength of a stale digest: re-downloaded.
        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD


class TestUnstableHashIsNotProof:
    """A hash only proves what is at the path if the file held still (F-242ae1)."""

    @staticmethod
    def _rewrite_after_hashing(local: Path) -> Any:
        real = integrity._sha256_file_sync

        def _hash_then_rewrite(path: Path) -> str:
            digest = real(path)  # the old, valid bytes
            local.write_bytes(b"Z" * len(_PAYLOAD))
            _bump_mtime(local)
            return digest

        return patch.object(integrity, "_sha256_file_sync", _hash_then_rewrite)

    async def test_single_file_fast_path_downloads_instead_of_reusing(
        self, tmp_path: Path
    ) -> None:
        store, local, ref = await _persisted(tmp_path)
        (tmp_path / "out.bin.sha256").unlink()  # force the fast path to hash

        with self._rewrite_after_hashing(local):
            out = await materialize_file_reference(store, ref)

        assert Path(out.local_path).read_bytes() == _PAYLOAD

    async def test_directory_fast_path_downloads_instead_of_reusing(
        self, tmp_path: Path
    ) -> None:
        store = create_memory_store()
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.bin").write_bytes(_PAYLOAD)
        ref = await persist_file_reference(store, FileReference.from_local(src))
        local = src / "a.bin"
        # Digest-only marker: not trusted on stat, so the file is hashed.
        (src / "a.bin.sha256").write_text(_sha(_PAYLOAD))

        with self._rewrite_after_hashing(local):
            out = await materialize_file_reference(store, ref)

        assert (Path(out.local_path) / "a.bin").read_bytes() == _PAYLOAD

    async def test_a_symlinked_file_that_held_still_is_still_reused(
        self, tmp_path: Path
    ) -> None:
        """Not trusted on stat, but a stable hash still proves it: no download."""
        store, local, ref = await _persisted(tmp_path)
        target = tmp_path / "target.bin"
        os.replace(local, target)
        local.symlink_to(target)
        (tmp_path / "out.bin.sha256").write_text(_sha(_PAYLOAD))

        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert out.ref is ref  # verified in place
        assert local.is_symlink()
        assert read_local_marker(str(local)).fingerprint is None  # type: ignore[union-attr]


class TestSymlinkSize:
    async def test_symlink_to_a_wrong_size_target_is_redownloaded(
        self, tmp_path: Path
    ) -> None:
        """A symlink is never trusted on stat, but its target's size is still checked."""
        store, local, ref = await _persisted(tmp_path)
        other = tmp_path / "other.bin"
        other.write_bytes(b"short")
        local.unlink()
        local.symlink_to(other)
        # A marker that agrees with the target's bytes, so only size can catch it.
        (tmp_path / "out.bin.sha256").write_text(_sha(b"short"))

        assert has_refs_to_materialize(_OneRef(ref=ref)) is True
        out = await materialize_file_refs(store, _OneRef(ref=ref))

        assert Path(out.ref.local_path).read_bytes() == _PAYLOAD


# ---------------------------------------------------------------------------
# Chunked download: hash as chunks land
# ---------------------------------------------------------------------------


class TestInOrderHasher:
    async def test_out_of_order_chunks_hash_to_the_file_digest(
        self, tmp_path: Path
    ) -> None:
        data = os.urandom(10_000)
        part = tmp_path / "f.part"
        part.write_bytes(data)
        done: set[int] = set()
        hasher = _InOrderHasher(part, size=len(data), chunk_size_bytes=1024, done=done)

        for idx in [3, 9, 0, 2, 1, 7, 5, 4, 8, 6]:
            done.add(idx)
            hasher.chunk_done()
            await asyncio.sleep(0)

        assert await hasher.finish() == _sha(data)

    async def test_abort_returns_no_digest(self, tmp_path: Path) -> None:
        part = tmp_path / "f.part"
        part.write_bytes(b"x" * 4096)
        hasher = _InOrderHasher(part, size=4096, chunk_size_bytes=1024, done={0})

        await hasher.abort()

        assert await hasher.finish() is None

    async def test_chunked_download_does_not_reread_the_finished_file(
        self, tmp_path: Path
    ) -> None:
        store = create_memory_store()
        data = os.urandom(64 * 1024)
        src = tmp_path / "src.bin"
        src.write_bytes(data)
        await upload_file("k/blob.bin", src, store, normalize=False)

        async def _no_reread(path: Path) -> str:
            raise AssertionError(f"re-read {path}; the digest was streamed")

        with patch.object(integrity, "sha256_file", _no_reread):
            digest = await download_file_chunked(
                "k/blob.bin",
                tmp_path / "dest.bin",
                store,
                chunk_size_bytes=4096,
                max_concurrent_chunks=4,
                compute_hash=True,
                normalize=False,
            )

        assert digest == _sha(data)
        assert (tmp_path / "dest.bin").read_bytes() == data

    async def test_cancelled_finish_leaves_the_feeder_running_until_abort(
        self, tmp_path: Path
    ) -> None:
        """Cancelling the caller must not orphan a thread reading the part file."""
        part = tmp_path / "f.part"
        part.write_bytes(b"x" * 4096)
        hasher = _InOrderHasher(
            part, size=4096, chunk_size_bytes=1024, done={0, 1, 2, 3}
        )
        started, release, finished = (threading.Event() for _ in range(3))
        real_feed = hasher._feed

        def _slow_feed(start: int, end: int) -> None:
            started.set()
            release.wait(5)
            real_feed(start, end)
            finished.set()

        hasher._feed = _slow_feed  # type: ignore[method-assign]
        waiter = asyncio.ensure_future(hasher.finish())
        assert await asyncio.to_thread(started.wait, 5)

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        aborting = asyncio.ensure_future(hasher.abort())

        # abort() sets _stop and then awaits the feeder in the same step, so
        # once _stop is set it is parked on the thread — checked from there,
        # not after a guessed delay.
        async def _parked() -> None:
            while not hasher._stop:
                await asyncio.sleep(0)

        await asyncio.wait_for(_parked(), 5)
        assert not aborting.done()  # still waiting for the thread

        release.set()
        await aborting
        assert finished.is_set()

    async def test_cancel_during_the_tail_hash_closes_the_part_fd(
        self, tmp_path: Path
    ) -> None:
        store = create_memory_store()
        data = os.urandom(16 * 1024)
        src = tmp_path / "src.bin"
        src.write_bytes(data)
        await upload_file("k/blob.bin", src, store, normalize=False)
        closed: list[int] = []
        real_close = os.close

        def _tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        async def _cancelled(self: _InOrderHasher) -> str | None:
            raise asyncio.CancelledError

        real_open = os.open
        opened: list[int] = []

        def _tracking_open(*args: Any, **kwargs: Any) -> int:
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with (
            patch.object(_InOrderHasher, "finish", _cancelled),
            patch("application_sdk.storage.chunked.os.open", _tracking_open),
            patch("application_sdk.storage.chunked.os.close", _tracking_close),
            pytest.raises(asyncio.CancelledError),
        ):
            await download_file_chunked(
                "k/blob.bin",
                tmp_path / "dest.bin",
                store,
                chunk_size_bytes=4096,
                max_concurrent_chunks=4,
                compute_hash=True,
                normalize=False,
            )

        assert opened and set(opened) <= set(closed)

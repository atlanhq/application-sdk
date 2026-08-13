"""Framework ``mark_progress()`` hooks on the ObjectStore transfer loops (FND-288).

Two grains are hooked, on purpose (ADR-0018):

* **per part / per chunk** inside ``ops.upload_file``, ``ops.download_file``,
  ``chunked._fetch_chunk`` and ``cloud.CloudStore.upload`` — so that a single
  multi-GB object cannot be one long quiet window;
* **per file** inside ``transfer._upload_one`` / ``_upload_from_store`` /
  ``_download_one`` and ``file_ref_sync._replace_refs`` — so the label an
  operator reads in a stall message says *which* loop went quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage.chunked import download_file_chunked
from application_sdk.storage.cloud import CloudStore
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import (
    materialize_file_refs,
    persist_file_refs,
)
from application_sdk.storage.ops import download_file, upload_file
from application_sdk.storage.transfer import download, upload
from tests.unit.conftest import RecordingProgressTracker

_PART = 1024


@pytest.fixture
def store():
    return create_memory_store()


def _local_file(tmp_path: Path, name: str, parts: int = 4) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (_PART * parts))
    return path


# ---------------------------------------------------------------------------
# ops.upload_file / ops.download_file — the byte loops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_marks_once_per_part(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    """A large single file must emit a signal per part, not one at the end."""
    src = _local_file(tmp_path, "big.bin", parts=4)

    await upload_file("k/big.bin", src, store, chunk_size=_PART, normalize=False)

    assert progress_marks.count("storage.upload_part") == 4


@pytest.mark.asyncio
async def test_a_zero_byte_upload_marks_nothing(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    """No parts written means no part-level progress to report."""
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    await upload_file("k/empty.bin", empty, store, normalize=False)

    assert progress_marks.count("storage.upload_part") == 0


@pytest.mark.asyncio
async def test_download_marks_per_streamed_chunk(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    src = _local_file(tmp_path, "src.bin", parts=4)
    await upload_file("k/src.bin", src, store, normalize=False)
    progress_marks.labels.clear()

    await download_file("k/src.bin", tmp_path / "out.bin", store, normalize=False)

    assert progress_marks.count("storage.download_chunk") >= 1
    assert (tmp_path / "out.bin").read_bytes() == src.read_bytes()


# ---------------------------------------------------------------------------
# chunked._fetch_chunk — the parallel range-GET path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunked_download_marks_once_per_range_get(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    src = _local_file(tmp_path, "ranged.bin", parts=4)
    await upload_file("k/ranged.bin", src, store, normalize=False)
    progress_marks.labels.clear()

    await download_file_chunked(
        "k/ranged.bin",
        tmp_path / "ranged-out.bin",
        store,
        chunk_size_bytes=_PART,
        normalize=False,
        resume=False,
    )

    assert progress_marks.count("storage.download_range") == 4
    assert (tmp_path / "ranged-out.bin").read_bytes() == src.read_bytes()


# ---------------------------------------------------------------------------
# transfer — the per-file boundaries, including skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directory_upload_marks_once_per_file(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    src_dir = tmp_path / "dir"
    for i in range(3):
        _local_file(src_dir, f"f{i}.bin", parts=1)

    await upload(str(src_dir), storage_path="prefix/dir/", store=store)

    assert progress_marks.count("storage.upload_file") == 3


@pytest.mark.asyncio
async def test_a_hash_match_skip_still_counts_as_progress(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    """An idempotent retry that skips every file is doing real work.

    Each skip costs a local digest plus a sidecar GET. Treating a long run of
    skips as silence would make a retry over a large prefix look like a stall.
    """
    src_dir = tmp_path / "dir"
    for i in range(3):
        _local_file(src_dir, f"f{i}.bin", parts=1)

    await upload(str(src_dir), storage_path="prefix/dir/", store=store)
    progress_marks.labels.clear()

    result = await upload(
        str(src_dir), storage_path="prefix/dir/", skip_if_exists=True, store=store
    )

    assert result.synced is False
    assert progress_marks.count("storage.upload_file") == 3
    assert progress_marks.count("storage.upload_part") == 0


@pytest.mark.asyncio
async def test_prefix_download_marks_once_per_file(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    src_dir = tmp_path / "dir"
    for i in range(3):
        _local_file(src_dir, f"f{i}.bin", parts=1)
    await upload(str(src_dir), storage_path="prefix/dir/", store=store)
    progress_marks.labels.clear()

    await download("prefix/dir/", str(tmp_path / "out"), store=store)

    assert progress_marks.count("storage.download_file") == 3


@pytest.mark.asyncio
async def test_cross_store_copy_marks_per_file(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """The SDR deployment-store fallback streams from one store to another."""
    source_store = create_memory_store()
    target_store = create_memory_store()
    src_dir = tmp_path / "dir"
    for i in range(2):
        _local_file(src_dir, f"f{i}.bin", parts=1)
    await upload(str(src_dir), storage_path="src/dir/", store=source_store)
    progress_marks.labels.clear()

    # local_path absent on this "pod" → the fallback branch streams from source.
    await upload(
        str(tmp_path / "missing"),
        storage_path="dst/dir/",
        store=target_store,
        _source_ref=FileReference(storage_path="src/dir/"),
        _source_store=source_store,
    )

    assert progress_marks.count("storage.copy_file") == 2


# ---------------------------------------------------------------------------
# file_ref_sync — the interceptor's typed-I/O boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_marks_once_per_reference(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    refs = [
        FileReference.from_local(str(_local_file(tmp_path, f"r{i}.bin", parts=1)))
        for i in range(3)
    ]

    await persist_file_refs(store, refs, output_path="artifacts/run")

    assert progress_marks.count("file_ref.persist") == 3


@pytest.mark.asyncio
async def test_materialize_marks_once_per_reference(
    tmp_path: Path, store, progress_marks: RecordingProgressTracker
) -> None:
    refs = [
        FileReference.from_local(
            str(_local_file(tmp_path, f"m{i}.bin", parts=1)), tier=StorageTier.RETAINED
        )
        for i in range(2)
    ]
    persisted = await persist_file_refs(store, refs, output_path="artifacts/run")
    # FileReference is immutable, so drop local_path via model_copy to force a
    # real download rather than a local-sidecar short-circuit.
    remote_only = [ref.model_copy(update={"local_path": None}) for ref in persisted]
    progress_marks.labels.clear()

    await materialize_file_refs(store, remote_only)

    assert progress_marks.count("file_ref.materialize") == 2


# ---------------------------------------------------------------------------
# CloudStore — external / customer-owned buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloudstore_upload_marks_once_per_part(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    cloud = CloudStore(create_memory_store())
    src = _local_file(tmp_path, "export.bin", parts=4)

    await cloud.upload(src, "exports/export.bin")

    # The assertion that matters is that an external upload marks at all: a
    # GB-class export to a customer bucket is the failure shape this covers,
    # and a small file is one part at the 8 MiB target part size.
    #
    # The label is ``storage.upload_part``, not ``cloudstore.upload_part``:
    # FND-306 routed CloudStore.upload through ``ops.upload_file`` so the
    # write-side integrity validations reach external buckets too, which
    # deleted this method's own part loop. The per-part hook came along with
    # the loop it lived in — the watchdog still sees progress, under the
    # primitive's label.
    assert progress_marks.count("storage.upload_part") >= 1


# ---------------------------------------------------------------------------
# Inertness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfers_behave_identically_with_no_tracker_bound(
    tmp_path: Path, store
) -> None:
    """No ``progress_marks`` fixture here: no tracker is bound, on purpose.

    Outside an activity ``current_progress_tracker()`` hands back the inert
    tracker, so every hook discards its signal and behaviour is unchanged.
    """
    src = _local_file(tmp_path, "inert.bin", parts=2)

    digest = await upload_file(
        "k/inert.bin", src, store, chunk_size=_PART, normalize=False
    )
    await download_file(
        "k/inert.bin", tmp_path / "inert-out.bin", store, normalize=False
    )

    assert digest is not None
    assert (tmp_path / "inert-out.bin").read_bytes() == src.read_bytes()

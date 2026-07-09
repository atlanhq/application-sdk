"""Opt-in large-file load tests for the storage transfer stack (BLDX-1513/1523).

Purpose: prove the slow-egress fixes hold at incident-and-beyond scale (a
single 50 GB file) by moving REAL bytes through the same public entry points
production uses — ``upload_file``, ``download_file_chunked``,
``materialize_file_reference`` (the incident's exact path), and
``download_prefix``. Nothing on the data path is mocked; the only patched
seam is the transport wrapper in the interruption test, which injects a
failure exactly the way a dying connection does.

Run (excluded from every default/CI suite; see tests/load/README.md):

    uv run poe load-test-storage                       # 1 GiB default
    ATLAN_LOAD_TEST_SIZE_MIB=51200 uv run poe load-test-storage   # 50 GiB

Every test asserts end-to-end sha256 equality, so a single corrupted or
misplaced byte fails the run.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from application_sdk.constants import (
    FILE_REF_CHUNK_CONCURRENCY,
    FILE_REF_CHUNK_SIZE_BYTES,
)
from application_sdk.contracts.types import FileReference
from application_sdk.storage.batch import download_prefix
from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import download_file_chunked, upload_file
from application_sdk.storage.reference import materialize_file_reference

from .conftest import (
    LOAD_SIZE_BYTES,
    LOAD_SIZE_MIB,
    Stopwatch,
    report,
    write_source_file,
)

pytestmark = pytest.mark.load


async def test_single_file_upload_then_chunked_download(local_store, tmp_path):
    """Full round trip at load size: multipart upload → parallel range-GET
    download with production chunking defaults → byte-exact sha match."""
    src = tmp_path / "src.bin"
    src_sha = write_source_file(src, LOAD_SIZE_BYTES)

    with Stopwatch() as up:
        up_sha = await upload_file("load/single.bin", src, local_store)
    report("upload_file", LOAD_SIZE_BYTES, up.elapsed)
    assert up_sha == src_sha
    src.unlink()  # halve peak disk usage before the download leg

    dest = tmp_path / "dest.bin"
    with Stopwatch() as down:
        dl_sha = await download_file_chunked(
            "load/single.bin",
            dest,
            local_store,
            chunk_size_bytes=FILE_REF_CHUNK_SIZE_BYTES,
            max_concurrent_chunks=FILE_REF_CHUNK_CONCURRENCY,
            compute_hash=True,
            normalize=False,
        )
    report("download_file_chunked", LOAD_SIZE_BYTES, down.elapsed)

    assert dl_sha == src_sha
    assert dest.stat().st_size == LOAD_SIZE_BYTES
    # Success removes the resume checkpoint — the file is observably complete.
    assert not (tmp_path / "dest.bin.transfer-state").exists()


async def test_interrupted_download_resumes_only_missing_ranges(local_store, tmp_path):
    """Kill the transport at ~60% of chunks, then retry: the retry must fetch
    ONLY the ranges the checkpoint doesn't cover and still produce a
    byte-exact file. This is the incident's activity-retry path at scale."""
    src = tmp_path / "src.bin"
    src_sha = write_source_file(src, LOAD_SIZE_BYTES)
    await upload_file("load/resume.bin", src, local_store)
    src.unlink()

    n_chunks = -(-LOAD_SIZE_BYTES // FILE_REF_CHUNK_SIZE_BYTES)
    fail_after = max(1, (n_chunks * 60) // 100)
    fail_from_offset = fail_after * FILE_REF_CHUNK_SIZE_BYTES

    import obstore

    real_get = obstore.get_async

    async def dying_get(st, key, **kw):
        # Deterministic by OFFSET (not call order): every range at or past the
        # 60% mark dies, everything before it transfers for real.
        rng = (kw.get("options") or {}).get("range")
        if rng and rng[0] >= fail_from_offset:
            raise RuntimeError("simulated egress death mid-transfer")
        return await real_get(st, key, **kw)

    dest = tmp_path / "dest.bin"
    with (
        patch("application_sdk.storage.ops.obstore.get_async", new=dying_get),
        pytest.raises(StorageError),
    ):
        await download_file_chunked(
            "load/resume.bin",
            dest,
            local_store,
            chunk_size_bytes=FILE_REF_CHUNK_SIZE_BYTES,
            # serialized so exactly the first 60% of chunks complete before
            # the failure — makes done_before deterministic
            max_concurrent_chunks=1,
            normalize=False,
        )

    state_path = tmp_path / "dest.bin.transfer-state"
    assert dest.exists() and state_path.exists()  # partial + checkpoint survive

    import orjson

    done_before = len(orjson.loads(state_path.read_bytes())["done"])
    assert done_before == fail_after

    fetched: list[int] = []

    async def counting_get(st, key, **kw):
        fetched.append(1)
        return await real_get(st, key, **kw)

    with Stopwatch() as resume:
        with patch("application_sdk.storage.ops.obstore.get_async", new=counting_get):
            dl_sha = await download_file_chunked(
                "load/resume.bin",
                dest,
                local_store,
                chunk_size_bytes=FILE_REF_CHUNK_SIZE_BYTES,
                max_concurrent_chunks=FILE_REF_CHUNK_CONCURRENCY,
                compute_hash=True,
                normalize=False,
            )
    report(
        f"resume ({n_chunks - done_before}/{n_chunks} chunks refetched)",
        (n_chunks - done_before) * FILE_REF_CHUNK_SIZE_BYTES,
        resume.elapsed,
    )

    assert len(fetched) == n_chunks - done_before
    assert dl_sha == src_sha
    assert not state_path.exists()


async def test_file_reference_materialize_single_file(local_store, tmp_path):
    """The incident path itself: FileReference materialization of a single
    load-size file, end to end through ``materialize_file_reference`` —
    HEAD-once, chunked, version-pinned, sha-verified by the sidecar protocol."""
    src = tmp_path / "src.bin"
    src_sha = write_source_file(src, LOAD_SIZE_BYTES)
    await upload_file("file_refs/load-single.bin", src, local_store)
    src.unlink()

    ref = FileReference(
        local_path=None,
        is_durable=True,
        storage_path="file_refs/load-single.bin",
    )
    with Stopwatch() as mat:
        result = await materialize_file_reference(
            local_store, ref, local_dir=str(tmp_path / "materialized")
        )
    report("materialize_file_reference", LOAD_SIZE_BYTES, mat.elapsed)

    assert result.local_path is not None
    from pathlib import Path

    out = Path(result.local_path)
    assert out.stat().st_size == LOAD_SIZE_BYTES
    h = hashlib.sha256()
    with out.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    assert h.hexdigest() == src_sha


async def test_download_prefix_bulk(local_store, tmp_path):
    """Bulk prefix download at load size split across 4 files — exercises the
    listing-supplied size+etag threading and per-file chunk dispatch."""
    per_file = LOAD_SIZE_BYTES // 4
    shas: dict[str, str] = {}
    for i in range(4):
        src = tmp_path / f"part{i}.bin"
        shas[f"load-prefix/part{i}.bin"] = write_source_file(src, per_file)
        await upload_file(f"load-prefix/part{i}.bin", src, local_store)
        src.unlink()

    out_dir = tmp_path / "prefix-out"
    with Stopwatch() as sw:
        dests = await download_prefix(
            "load-prefix/", out_dir, local_store, normalize=False
        )
    report(
        f"download_prefix (4 × {LOAD_SIZE_MIB // 4} MiB)", LOAD_SIZE_BYTES, sw.elapsed
    )

    assert len(dests) == 4
    from pathlib import Path

    for dest in dests:
        p = Path(dest)
        key = str(p.relative_to(out_dir))
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        assert h.hexdigest() == shas[key], f"content mismatch for {key}"

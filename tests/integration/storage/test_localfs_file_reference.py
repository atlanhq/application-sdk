"""FileReference persist / materialize round trips against a local objectstore.

Covers the durable-reference lifecycle end-to-end on a real ``LocalStore``:
persist (ephemeral → durable, with SHA-256 sidecars), materialize (durable →
local, with integrity verification), the sidecar cache-hit fast path, and the
typed not-found branch for dangling references.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.contracts.types import FileReference
from application_sdk.storage.errors import StorageNotFoundError
from application_sdk.storage.ops import exists
from application_sdk.storage.reference import (
    materialize_file_reference,
    persist_file_reference,
)


@pytest.mark.integration
async def test_single_file_persist_materialize_roundtrip(local_store, tmp_path):
    """persist → delete local → materialize restores identical content."""
    src = tmp_path / "payload.json"
    src.write_text('{"answer": 42}')

    ref = FileReference.from_local(src)
    assert ref.is_durable is False

    durable = await persist_file_reference(
        local_store, ref, key="artifacts/refs/payload.json"
    )
    assert durable.is_durable is True
    assert durable.storage_path == "artifacts/refs/payload.json"
    # persist writes a SHA-256 sidecar next to the object.
    assert await exists("artifacts/refs/payload.json.sha256", local_store) is True

    # Simulate a different worker: the local copy (and its sidecar) are gone.
    src.unlink()
    Path(str(src) + ".sha256").unlink(missing_ok=True)

    restored = await materialize_file_reference(local_store, durable)
    assert restored.local_path is not None
    assert Path(restored.local_path).read_text() == '{"answer": 42}'
    # materialize re-creates the local sidecar for future cache hits.
    assert Path(restored.local_path + ".sha256").exists()


@pytest.mark.integration
async def test_materialize_cache_hit_skips_redownload(local_store, tmp_path):
    """A second materialize with an intact local file is a sidecar cache hit."""
    src = tmp_path / "cached.txt"
    src.write_text("cache me")

    durable = await persist_file_reference(
        local_store, FileReference.from_local(src), key="artifacts/refs/cached.txt"
    )

    first = await materialize_file_reference(local_store, durable)
    # Tamper detection baseline: mtime of the local file before second call.
    local = Path(first.local_path)
    before = local.stat().st_mtime_ns

    second = await materialize_file_reference(local_store, durable)
    assert second.local_path == first.local_path
    assert Path(second.local_path).read_text() == "cache me"
    # Cache hit: the local file was not rewritten.
    assert local.stat().st_mtime_ns == before


@pytest.mark.integration
async def test_materialize_redownloads_when_local_copy_is_corrupted(
    local_store, tmp_path
):
    """A local copy that no longer matches the stored sidecar is re-downloaded."""
    src = tmp_path / "healme.txt"
    src.write_text("original")

    durable = await persist_file_reference(
        local_store, FileReference.from_local(src), key="artifacts/refs/healme.txt"
    )

    # Corrupt the local copy in place.
    src.write_text("corrupted")

    restored = await materialize_file_reference(local_store, durable)
    assert Path(restored.local_path).read_text() == "original"


@pytest.mark.integration
async def test_directory_persist_materialize_roundtrip(local_store, tmp_path):
    """Directory references round-trip the whole tree with per-file sidecars."""
    src_dir = tmp_path / "bundle"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "a.txt").write_text("alpha")
    (src_dir / "sub" / "b.txt").write_text("beta")

    ref = FileReference.from_local(src_dir)
    assert ref.file_count == 2

    durable = await persist_file_reference(
        local_store, ref, output_path="artifacts/run-9"
    )
    assert durable.is_durable is True
    assert durable.file_count == 2

    dest_dir = tmp_path / "restored-bundle"
    restored = await materialize_file_reference(
        local_store,
        FileReference(
            storage_path=durable.storage_path, is_durable=True, local_path=str(dest_dir)
        ),
    )
    assert restored.file_count == 2
    assert (dest_dir / "a.txt").read_text() == "alpha"
    assert (dest_dir / "sub" / "b.txt").read_text() == "beta"


@pytest.mark.integration
async def test_materialize_dangling_reference_raises_typed_not_found(local_store):
    """Materializing a durable ref whose object is gone raises the typed error."""
    ghost = FileReference(storage_path="artifacts/refs/ghost.txt", is_durable=True)
    with pytest.raises(StorageNotFoundError) as excinfo:
        await materialize_file_reference(local_store, ghost)
    assert excinfo.value.key == "artifacts/refs/ghost.txt"

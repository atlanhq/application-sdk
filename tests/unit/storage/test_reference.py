"""Unit tests for materialize_file_reference using MemoryStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.contracts.types import FileReference
from application_sdk.storage.errors import StorageNotFoundError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _put
from application_sdk.storage.reference import (
    materialize_file_reference,
    persist_file_reference,
)


@pytest.fixture
def store():
    return create_memory_store()


class TestMaterializeFileReferenceEmptyPrefix:
    """Empty-list-from-list_keys must not collapse into a misleading
    download_file on the bare prefix.

    Before the fix, a `FileReference` whose `storage_path` resolved to a
    prefix with no objects (because the writer hasn't run, or because the
    store credentials don't have list/read permission on that prefix) would
    fall into the single-file branch and call `download_file(prefix, ...)`
    on the bare path. That GET returns 404 from the underlying store and
    surfaces as a generic "Key not found in store: <prefix>" error,
    obscuring the real cause.
    """

    async def test_empty_prefix_with_missing_key_raises_descriptive_error(
        self, store
    ) -> None:
        ref = FileReference(
            storage_path="argo-artifacts/missing/run-123",
            is_durable=True,
        )
        with pytest.raises(StorageNotFoundError) as exc_info:
            await materialize_file_reference(store, ref)

        err = str(exc_info.value)
        # Path is mentioned so operators can see what was missing.
        assert "argo-artifacts/missing/run-123" in err
        # Disambiguates from the generic "Key not found in store" message
        # by naming the empty-prefix case explicitly.
        msg = err.lower()
        assert ("no objects" in msg) or ("no single file" in msg)


class TestMaterializeFileReferenceRegressions:
    """Cases that already worked must keep working after the HEAD check."""

    async def test_directory_prefix_downloads_all_files(
        self, store, tmp_path: Path
    ) -> None:
        await _put("dir/a.txt", b"alpha", store)
        await _put("dir/sub/b.txt", b"beta", store)

        ref = FileReference(storage_path="dir", is_durable=True)
        out = await materialize_file_reference(store, ref, local_dir=str(tmp_path))

        assert out.local_path is not None
        local = Path(out.local_path)
        assert (local / "a.txt").read_bytes() == b"alpha"
        assert (local / "sub" / "b.txt").read_bytes() == b"beta"

    async def test_single_file_at_exact_key_downloads(
        self, store, tmp_path: Path
    ) -> None:
        # Listing "solo.txt/" returns empty because list_keys appends a
        # trailing slash; the function must HEAD the bare key, see it
        # exists, and proceed with download_file.
        await _put("solo.txt", b"hello", store)

        ref = FileReference(storage_path="solo.txt", is_durable=True)
        out = await materialize_file_reference(store, ref, local_dir=str(tmp_path))

        assert out.local_path is not None
        assert Path(out.local_path).read_bytes() == b"hello"

    async def test_local_fast_path_skips_head_and_download(
        self, store, tmp_path: Path
    ) -> None:
        # Persist a file so the store has both the file and its sidecar.
        f = tmp_path / "data.txt"
        f.write_bytes(b"payload")

        ephemeral = FileReference(local_path=str(f), is_durable=False)
        durable = await persist_file_reference(store, ephemeral, key="cached/data.txt")

        # Materialise — the local file already matches the stored sidecar
        # so the function must return early without touching the store
        # for the bare key (i.e. no HEAD/GET that would 404 on a different
        # path).
        out = await materialize_file_reference(store, durable)
        assert out.local_path == str(f)
        assert Path(out.local_path).read_bytes() == b"payload"

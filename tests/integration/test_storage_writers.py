"""Integration tests for the writer migration to the FileReference contract.

Verifies the full flow against a real local obstore (no mocks, no Temporal):

* `RollingFileWriter` → close → `persist_file_reference` → assert keys + sidecars
  land in the store under the persisted prefix.
* `ParquetFileWriter(defer_uploads=True)` → close → `persist_file_reference` →
  assert files land in the store, no inline uploads happened during writes.
* `ParquetFileWriter(defer_uploads=False)` → close → assert main's behaviour is
  preserved: inline uploads happen at the writer's local-path-mirrored keys,
  and `result.files` is `None`.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pandas as pd
import pytest

from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.storage.batch import list_keys
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.formats.parquet import ParquetFileWriter
from application_sdk.storage.reference import persist_file_reference
from application_sdk.storage.rolling import RollingFileWriter


@pytest.fixture
def store(tmp_path):
    """Real local object store backed by a temp directory."""
    return create_local_store(tmp_path / "store")


@pytest.fixture
def infra(store):
    """Set an InfrastructureContext so inline _upload_file resolves a store.

    Used only by tests that exercise the legacy inline-upload path (default
    ``defer_uploads=False`` on ParquetFileWriter). Tests that use
    ``persist_file_reference`` directly pass the store explicitly and do not
    need this fixture.
    """
    set_infrastructure(InfrastructureContext(storage=store))
    yield store
    clear_infrastructure()


# ------------------------------------------------------------------
# RollingFileWriter — end-to-end with persist_file_reference
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_rolling_writer_writes_chunks_and_persists_to_store(
    store, tmp_path: Path
):
    """RollingFileWriter chunks → close → persist → store has all chunk keys.

    Forces a small `chunk_interval_seconds` and uses real time so we exercise
    the rollover path. Asserts every disk chunk lands in the store under the
    persisted prefix with a `.sha256` sidecar.
    """
    import asyncio

    def _flush_jsonl(batches: list[list[dict]], path: str) -> None:
        with open(path, "w") as f:
            for batch in batches:
                f.writelines(f"{record}\n" for record in batch)

    async with RollingFileWriter[list[dict]](
        base_path=str(tmp_path / "extract"),
        extension=".json",
        flush_fn=_flush_jsonl,
        chunk_interval_seconds=0.01,
        scoped_subdir_name="run",
    ) as writer:
        await writer.append([{"id": 1}, {"id": 2}])
        await asyncio.sleep(0.05)  # cross interval
        await writer.append([{"id": 3}, {"id": 4}])
        await asyncio.sleep(0.05)
        await writer.append([{"id": 5}])

    # Disk: every chunk must exist under the writer-owned subdir.
    local_chunks = sorted(Path(writer.output_dir).glob("*.json"))
    assert (
        len(local_chunks) >= 2
    ), f"expected ≥2 rolled chunks; got {len(local_chunks)}: {local_chunks}"

    # Persist through the FileReference boundary and confirm the store
    # mirrors the writer-owned subdir (one key per chunk + a sidecar each).
    durable = await persist_file_reference(store, writer.file_reference)
    assert durable.is_durable
    assert durable.storage_path is not None

    chunk_keys = await list_keys(durable.storage_path, store, suffix=".json")
    sidecar_keys = await list_keys(durable.storage_path, store, suffix=".sha256")
    assert len(chunk_keys) == len(local_chunks)
    assert len(sidecar_keys) == len(local_chunks)


@pytest.mark.integration
async def test_rolling_writer_file_reference_scoped_to_subdir(store, tmp_path: Path):
    """FileReference must scope strictly to the writer's subdir.

    Pre-create a sibling file outside the writer's subdir; persist the ref;
    confirm the sibling does not appear in the store.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    sibling = shared / "do_not_upload.txt"
    sibling.write_text("hands off")

    def _flush(batches, path):
        with open(path, "w") as f:
            f.write(str(batches))

    async with RollingFileWriter(
        base_path=str(shared),
        extension=".json",
        flush_fn=_flush,
        scoped_subdir_name="run",
    ) as writer:
        await writer.append({"id": 1})

    durable = await persist_file_reference(store, writer.file_reference)

    # Sidecars + chunk files only — never the sibling.
    all_keys = await list_keys(durable.storage_path, store)
    assert all(
        "do_not_upload" not in k for k in all_keys
    ), f"sibling leaked into store: {all_keys}"
    assert any(k.endswith(".json") for k in all_keys)


# ------------------------------------------------------------------
# ParquetFileWriter(defer_uploads=True) — end-to-end
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_parquet_writer_deferred_persists_via_file_reference(
    store, tmp_path: Path
):
    """defer_uploads=True: writer leaves files on disk; persist_file_reference
    is what actually puts them in the store."""
    df = pd.DataFrame({"id": list(range(120))})  # 120 rows / 50 = 3 sub-chunks

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="orders",
            buffer_size=50,
            defer_uploads=True,
        )
        await writer.write(df)
        result = await writer.close()

    # Before persist: writer-owned subdir on disk, store still empty.
    assert result.files is not None
    assert result.files.is_durable is False
    assert os.path.isdir(result.files.local_path)
    pre_persist_keys = await list_keys("", store)
    assert pre_persist_keys == []

    # After persist: parquet chunks + statistics + sidecars are in the store.
    durable = await persist_file_reference(store, result.files)
    parquet_keys = await list_keys(durable.storage_path, store, suffix=".parquet")
    sidecar_keys = await list_keys(durable.storage_path, store, suffix=".sha256")
    all_keys = await list_keys(durable.storage_path, store)
    assert len(parquet_keys) >= 3, f"expected ≥3 parquet chunks; got {parquet_keys}"
    assert any(
        "statistics" in k for k in all_keys
    ), f"statistics sidecar missing from {all_keys}"
    # One sidecar per file (parquet + statistics).
    assert len(sidecar_keys) == len(all_keys) - len(sidecar_keys), (
        f"sidecar count mismatch: data={len(all_keys) - len(sidecar_keys)} "
        f"sidecars={len(sidecar_keys)}"
    )


# ------------------------------------------------------------------
# ParquetFileWriter(defer_uploads=False) — main behaviour preserved
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_parquet_writer_default_mode_uploads_inline(infra, tmp_path: Path):
    """defer_uploads=False (default) must behave like main:
    - Inline uploads at writer-path-mirrored keys during write/close.
    - close() returns result.files == None.
    - No persist_file_reference call required for the data to be in the store.
    """
    df = pd.DataFrame({"id": list(range(120))})

    base = str(tmp_path / "out")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        writer = ParquetFileWriter(
            path=base,
            typename="orders",
            buffer_size=50,
            # No defer_uploads → main's inline-upload path.
        )
        await writer.write(df)
        result = await writer.close()

    # Backwards-compatible return contract.
    assert result.files is None, "default mode must not expose a FileReference"
    assert result.total_record_count == 120

    # Inline uploads landed at keys mirroring the writer's local path.
    keys = await list_keys(os.path.join(base, "orders"), infra, suffix=".parquet")
    assert len(keys) >= 3, f"expected ≥3 inline-uploaded chunks; got {keys}"

    # Statistics sidecar also uploaded inline.
    stat_keys = await list_keys(os.path.join(base, "orders"), infra)
    assert any(
        "statistics" in k for k in stat_keys
    ), f"statistics file missing from {stat_keys}"

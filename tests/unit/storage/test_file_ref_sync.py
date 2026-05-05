"""Unit tests for transparent FileReference persist / materialise."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated

from pydantic import Field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, Lazy
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import (
    has_refs_to_materialize,
    has_refs_to_persist,
    materialize_file_refs,
    persist_file_refs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_local_file(content: bytes = b"hello") -> str:
    """Write *content* to a temp file and return the path."""
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    return path


class _TaskOutput(Output, allow_unbounded_fields=True):
    name: str = ""
    ref: FileReference = Field(default_factory=FileReference)


class _TaskInput(Input, allow_unbounded_fields=True):
    ref: FileReference = Field(default_factory=FileReference)


# ---------------------------------------------------------------------------
# has_refs_to_persist
# ---------------------------------------------------------------------------


class TestHasRefsToPersist:
    def test_returns_false_for_no_refs(self) -> None:
        output = _TaskOutput(name="test")
        assert has_refs_to_persist(output) is False

    def test_returns_true_for_ephemeral_ref(self) -> None:
        path = _make_local_file()
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            assert has_refs_to_persist(output) is True
        finally:
            os.unlink(path)

    def test_returns_false_for_already_durable(self) -> None:
        output = _TaskOutput(
            ref=FileReference(
                local_path="/tmp/x", is_durable=True, storage_path="file_refs/abc"
            )
        )
        assert has_refs_to_persist(output) is False

    def test_returns_false_for_ref_with_no_local_path(self) -> None:
        output = _TaskOutput(ref=FileReference(is_durable=False, local_path=None))
        assert has_refs_to_persist(output) is False


# ---------------------------------------------------------------------------
# has_refs_to_materialize
# ---------------------------------------------------------------------------


class TestHasRefsToMaterialize:
    def test_returns_false_for_no_refs(self) -> None:
        inp = _TaskInput()
        assert has_refs_to_materialize(inp) is False

    def test_returns_true_for_durable_ref_without_local_path(self) -> None:
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path="file_refs/abc", local_path=None
            )
        )
        assert has_refs_to_materialize(inp) is True

    def test_returns_true_for_durable_ref_with_missing_local_file(self) -> None:
        # local_path is set but the file doesn't exist on disk → needs materialize
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True,
                storage_path="file_refs/abc",
                local_path="/tmp/nonexistent_file_that_does_not_exist",
            )
        )
        assert has_refs_to_materialize(inp) is True

    def test_returns_false_for_durable_ref_with_valid_sidecar(self) -> None:
        import hashlib

        content = b"verified content"
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, content)
            os.close(fd)
            digest = hashlib.sha256(content).hexdigest()
            Path(path + ".sha256").write_text(digest)
            inp = _TaskInput(
                ref=FileReference(
                    is_durable=True, storage_path="file_refs/abc", local_path=path
                )
            )
            assert has_refs_to_materialize(inp) is False
        finally:
            os.unlink(path)
            try:
                os.unlink(path + ".sha256")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# persist_file_refs round-trip
# ---------------------------------------------------------------------------


class TestPersistAndMaterialize:
    async def test_persist_uploads_file(self) -> None:
        store = create_memory_store()
        content = b"payload data"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)
            assert isinstance(persisted, _TaskOutput)
            assert persisted.ref.is_durable is True
            assert persisted.ref.storage_path is not None
            assert persisted.ref.local_path == path  # local_path preserved
        finally:
            os.unlink(path)

    async def test_materialize_downloads_file(self) -> None:
        store = create_memory_store()
        content = b"round-trip content"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)
            storage_path = persisted.ref.storage_path
            assert storage_path is not None
        finally:
            os.unlink(path)

        # Simulate a remote worker that has no local file
        remote_input = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            )
        )
        materialised = await materialize_file_refs(store, remote_input)
        assert isinstance(materialised, _TaskInput)
        assert materialised.ref.local_path is not None
        assert os.path.exists(materialised.ref.local_path)
        assert open(materialised.ref.local_path, "rb").read() == content
        os.unlink(materialised.ref.local_path)

    async def test_persist_noop_for_durable_ref(self) -> None:
        store = create_memory_store()
        ref = FileReference(is_durable=True, storage_path="file_refs/existing")
        output = _TaskOutput(ref=ref)
        result = await persist_file_refs(store, output)
        assert result.ref.storage_path == "file_refs/existing"

    async def test_materialize_noop_for_non_durable(self) -> None:
        store = create_memory_store()
        inp = _TaskInput(ref=FileReference(is_durable=False, local_path="/tmp/local"))
        result = await materialize_file_refs(store, inp)
        assert result.ref.local_path == "/tmp/local"

    async def test_materialize_fast_path_skips_download(self) -> None:
        """Local file intact + sidecar match → no re-download."""
        store = create_memory_store()
        content = b"fast path data"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)

            # Materialize onto the same worker (local file still present).
            inp = _TaskInput(ref=persisted.ref)
            materialised = await materialize_file_refs(store, inp)
            assert materialised.ref.local_path == path
            assert open(path, "rb").read() == content
        finally:
            os.unlink(path)


class TestDirectoryPersistMaterialize:
    async def test_persist_directory_sets_file_count(self, tmp_path) -> None:
        store = create_memory_store()
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")

        ref = FileReference(local_path=str(tmp_path), is_durable=False)
        output = _TaskOutput(ref=ref)
        persisted = await persist_file_refs(store, output)

        assert persisted.ref.is_durable is True
        assert persisted.ref.file_count == 3
        assert persisted.ref.storage_path is not None
        assert persisted.ref.storage_path.endswith("/")

    async def test_persist_directory_roundtrip(self, tmp_path) -> None:
        """Persist a directory then materialize on a 'different worker'."""
        store = create_memory_store()
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.txt").write_bytes(b"x content")
        (src / "y.txt").write_bytes(b"y content")

        ref = FileReference(local_path=str(src), is_durable=False)
        output = _TaskOutput(ref=ref)
        persisted = await persist_file_refs(store, output)
        storage_path = persisted.ref.storage_path

        # Simulate a different worker — no local_path.
        remote_input = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            )
        )
        materialised = await materialize_file_refs(store, remote_input)

        assert materialised.ref.local_path is not None
        local_dir = materialised.ref.local_path
        assert os.path.isdir(local_dir)
        assert (tmp_path / local_dir.lstrip("/") / "x.txt").exists() or open(
            os.path.join(local_dir, "x.txt"), "rb"
        ).read() == b"x content"
        assert materialised.ref.file_count == 2

    async def test_local_sidecar_ok_returns_false_for_directory(self, tmp_path) -> None:
        from application_sdk.storage.file_ref_sync import _local_sidecar_ok

        assert _local_sidecar_ok(str(tmp_path)) is False

    async def test_needs_materialize_true_for_dir_ref(self, tmp_path) -> None:
        from application_sdk.storage.file_ref_sync import _needs_materialize

        ref = FileReference(
            is_durable=True,
            storage_path="file_refs/abc/",
            local_path=str(tmp_path),
        )
        # Directory always needs materialize (no fast path).
        assert _needs_materialize(ref) is True


# ---------------------------------------------------------------------------
# Deduplication: same storage_path downloaded exactly once
# ---------------------------------------------------------------------------


class _MultiRefInput(Input, allow_unbounded_fields=True):
    ref_a: FileReference = Field(default_factory=FileReference)
    ref_b: FileReference = Field(default_factory=FileReference)
    ref_c: FileReference = Field(default_factory=FileReference)


class TestMaterializeDedup:
    async def test_same_storage_path_downloaded_once(self) -> None:
        """Three refs sharing a storage_path → one download, all get local_path."""
        store = create_memory_store()
        content = b"shared payload"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)
            storage_path = persisted.ref.storage_path
            assert storage_path is not None
        finally:
            os.unlink(path)

        # Three refs all point at the same remote storage_path, none have local files.
        inp = _MultiRefInput(
            ref_a=FileReference(is_durable=True, storage_path=storage_path),
            ref_b=FileReference(is_durable=True, storage_path=storage_path),
            ref_c=FileReference(is_durable=True, storage_path=storage_path),
        )

        download_count = 0
        original_materialize = None

        from application_sdk.storage import reference as _ref_mod

        original_materialize = _ref_mod.materialize_file_reference

        async def _counting_materialize(store, ref, **kwargs):
            nonlocal download_count
            download_count += 1
            return await original_materialize(store, ref, **kwargs)

        _ref_mod.materialize_file_reference = _counting_materialize
        try:
            result = await materialize_file_refs(store, inp)
        finally:
            _ref_mod.materialize_file_reference = original_materialize

        # Only one actual download despite three refs.
        assert download_count == 1

        # All three fields have a local_path set and the content is correct.
        for field_name in ("ref_a", "ref_b", "ref_c"):
            local = getattr(result, field_name).local_path
            assert local is not None, f"{field_name}.local_path is None"
            assert os.path.exists(local), f"{field_name}.local_path does not exist"
            assert open(local, "rb").read() == content

    async def test_different_storage_paths_both_downloaded(self) -> None:
        """Two refs with different storage_paths → two independent downloads."""
        store = create_memory_store()
        content_a = b"payload A"
        content_b = b"payload B"
        path_a = _make_local_file(content_a)
        path_b = _make_local_file(content_b)
        try:
            p_a = await persist_file_refs(
                store,
                _TaskOutput(ref=FileReference(local_path=path_a, is_durable=False)),
            )
            p_b = await persist_file_refs(
                store,
                _TaskOutput(ref=FileReference(local_path=path_b, is_durable=False)),
            )
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

        inp = _MultiRefInput(
            ref_a=FileReference(is_durable=True, storage_path=p_a.ref.storage_path),
            ref_b=FileReference(is_durable=True, storage_path=p_b.ref.storage_path),
            ref_c=FileReference(is_durable=True, storage_path=p_a.ref.storage_path),
        )

        result = await materialize_file_refs(store, inp)

        assert open(result.ref_a.local_path, "rb").read() == content_a
        assert open(result.ref_b.local_path, "rb").read() == content_b
        # ref_c shares storage_path with ref_a — same content.
        assert open(result.ref_c.local_path, "rb").read() == content_a


# ---------------------------------------------------------------------------
# Concurrency: refs are processed concurrently, not sequentially
# ---------------------------------------------------------------------------


class TestMaterializeConcurrency:
    async def test_concurrent_downloads_complete(self) -> None:
        """Multiple distinct refs complete when run concurrently."""

        store = create_memory_store()
        paths = [_make_local_file(f"content {i}".encode()) for i in range(4)]
        storage_paths = []
        try:
            for p in paths:
                persisted = await persist_file_refs(
                    store,
                    _TaskOutput(ref=FileReference(local_path=p, is_durable=False)),
                )
                storage_paths.append(persisted.ref.storage_path)
        finally:
            for p in paths:
                os.unlink(p)

        # Build an input with 4 distinct durable refs.
        class _FourRefInput(Input, allow_unbounded_fields=True):
            r0: FileReference = Field(default_factory=FileReference)
            r1: FileReference = Field(default_factory=FileReference)
            r2: FileReference = Field(default_factory=FileReference)
            r3: FileReference = Field(default_factory=FileReference)

        inp = _FourRefInput(
            r0=FileReference(is_durable=True, storage_path=storage_paths[0]),
            r1=FileReference(is_durable=True, storage_path=storage_paths[1]),
            r2=FileReference(is_durable=True, storage_path=storage_paths[2]),
            r3=FileReference(is_durable=True, storage_path=storage_paths[3]),
        )

        result = await materialize_file_refs(store, inp)

        for i, field in enumerate(("r0", "r1", "r2", "r3")):
            local = getattr(result, field).local_path
            assert local is not None
            assert open(local, "rb").read() == f"content {i}".encode()


# ---------------------------------------------------------------------------
# Lazy materialization: Lazy-marked fields are left as durable refs
# ---------------------------------------------------------------------------


class _LazyInput(Input, allow_unbounded_fields=True):
    eager: FileReference = Field(default_factory=FileReference)
    lazy: Annotated[FileReference, Lazy()] = Field(default_factory=FileReference)


class TestLazyMaterialization:
    async def test_lazy_field_not_downloaded(self) -> None:
        """Eager field is materialized; Lazy-marked field is left as durable ref."""
        store = create_memory_store()
        content = b"lazy test payload"
        path = _make_local_file(content)
        try:
            from application_sdk.storage.file_ref_sync import persist_file_refs

            persisted = await persist_file_refs(
                store,
                _TaskOutput(ref=FileReference(local_path=path, is_durable=False)),
            )
            storage_path = persisted.ref.storage_path
            assert storage_path is not None
        finally:
            os.unlink(path)

        inp = _LazyInput(
            eager=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            ),
            lazy=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            ),
        )

        result = await materialize_file_refs(store, inp)

        # Eager field: downloaded and local_path set.
        assert result.eager.local_path is not None
        assert os.path.exists(result.eager.local_path)
        assert open(result.eager.local_path, "rb").read() == content

        # Lazy field: still durable, local_path unchanged (still None).
        assert result.lazy.is_durable is True
        assert result.lazy.local_path is None
        assert result.lazy.storage_path == storage_path

    async def test_lazy_field_stays_durable_when_sibling_downloads_same_path(
        self,
    ) -> None:
        """Even when eager sibling downloads the same storage_path, lazy field stays durable."""
        store = create_memory_store()
        content = b"shared lazy sibling payload"
        path = _make_local_file(content)
        try:
            from application_sdk.storage.file_ref_sync import persist_file_refs

            persisted = await persist_file_refs(
                store,
                _TaskOutput(ref=FileReference(local_path=path, is_durable=False)),
            )
            storage_path = persisted.ref.storage_path
            assert storage_path is not None
        finally:
            os.unlink(path)

        inp = _LazyInput(
            eager=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            ),
            lazy=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            ),
        )

        result = await materialize_file_refs(store, inp)

        # Eager sibling was downloaded.
        assert result.eager.local_path is not None

        # Lazy field is untouched — still durable, no local_path.
        assert result.lazy.is_durable is True
        assert result.lazy.local_path is None

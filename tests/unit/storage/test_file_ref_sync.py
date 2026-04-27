"""Unit tests for transparent FileReference persist / materialise."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from pydantic import Field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
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
# auto_materialize=False escape hatch (BLDX-1155)
# ---------------------------------------------------------------------------


class TestAutoMaterializeOptOut:
    """When ``auto_materialize=False`` the interceptor must skip the ref."""

    def test_has_refs_to_persist_skips_opted_out_refs(self) -> None:
        path = _make_local_file()
        try:
            output = _TaskOutput(
                ref=FileReference(
                    local_path=path, is_durable=False, auto_materialize=False
                )
            )
            assert has_refs_to_persist(output) is False
        finally:
            os.unlink(path)

    def test_has_refs_to_materialize_skips_opted_out_refs(self) -> None:
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True,
                storage_path="file_refs/abc",
                local_path=None,
                auto_materialize=False,
            )
        )
        assert has_refs_to_materialize(inp) is False

    async def test_persist_does_not_upload_opted_out_refs(self) -> None:
        store = create_memory_store()
        path = _make_local_file(b"opt-out payload")
        try:
            output = _TaskOutput(
                ref=FileReference(
                    local_path=path, is_durable=False, auto_materialize=False
                )
            )
            persisted = await persist_file_refs(store, output)
            # Ref must come back unchanged — still ephemeral, no storage_path.
            assert persisted.ref.is_durable is False
            assert persisted.ref.storage_path is None
            assert persisted.ref.auto_materialize is False
        finally:
            os.unlink(path)

    async def test_materialize_does_not_download_opted_out_refs(self) -> None:
        store = create_memory_store()
        # Storage path doesn't even need to exist — interceptor must not call.
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True,
                storage_path="file_refs/never-uploaded",
                local_path=None,
                auto_materialize=False,
            )
        )
        result = await materialize_file_refs(store, inp)
        # Ref must be untouched: no local_path was filled in.
        assert result.ref.local_path is None
        assert result.ref.auto_materialize is False

    async def test_mixed_refs_only_opted_in_are_processed(self) -> None:
        """When two refs share an Output and one is opted out, only the other is uploaded."""
        store = create_memory_store()
        opt_in_path = _make_local_file(b"opt-in")
        opt_out_path = _make_local_file(b"opt-out")
        try:

            class _DualOutput(Output, allow_unbounded_fields=True):
                opt_in: FileReference
                opt_out: FileReference

            output = _DualOutput(
                opt_in=FileReference(local_path=opt_in_path, is_durable=False),
                opt_out=FileReference(
                    local_path=opt_out_path,
                    is_durable=False,
                    auto_materialize=False,
                ),
            )
            persisted = await persist_file_refs(store, output)
            assert persisted.opt_in.is_durable is True
            assert persisted.opt_in.storage_path is not None
            assert persisted.opt_out.is_durable is False
            assert persisted.opt_out.storage_path is None
        finally:
            os.unlink(opt_in_path)
            os.unlink(opt_out_path)


# ---------------------------------------------------------------------------
# mkstemp cleanup on failure (BLDX-1155 #5: don't orphan temp files)
# ---------------------------------------------------------------------------


class TestMaterializeTempCleanup:
    """When a materialize fails after mkstemp, the temp file must be unlinked.

    Today a network failure mid-download orphans an empty temp file in the
    system temp directory.  Across thousands of retries that fills disk and
    eventually evicts the worker pod for reasons unrelated to the original
    failure.
    """

    async def test_temp_file_unlinked_when_download_fails(
        self, monkeypatch, tmp_path
    ) -> None:
        from pathlib import Path

        from application_sdk.storage import reference as reference_module
        from application_sdk.storage.errors import StorageError
        from application_sdk.storage.factory import create_memory_store

        store = create_memory_store()

        # Simulate "ref already in store, but download_file raises" — i.e. we
        # made it past the listing stage and into mkstemp + download.
        ref = FileReference(
            is_durable=True,
            storage_path="file_refs/never-finishes.bin",
            local_path=None,
        )

        # Stub list_keys() so the function takes the single-file branch.
        async def _fake_list_keys(*_args, **_kwargs):
            return []  # empty data_keys → single-file path

        # Stub download_file() to raise mid-way (simulates network blip).
        async def _failing_download(*_args, **_kwargs):
            raise StorageError("simulated network failure", key="file_refs/x")

        # The function imports both lazily inside its body, so patch the
        # source modules they're resolved from.
        from application_sdk.storage import batch as batch_module
        from application_sdk.storage import ops as ops_module

        monkeypatch.setattr(batch_module, "list_keys", _fake_list_keys)
        monkeypatch.setattr(ops_module, "download_file", _failing_download)

        # Snapshot the temp dir before so we can compare.
        local_dir = str(tmp_path)
        before = set(Path(local_dir).iterdir())

        with pytest.raises(StorageError):
            await reference_module.materialize_file_reference(
                store, ref, local_dir=local_dir
            )

        after = set(Path(local_dir).iterdir())
        leaked = after - before
        assert not leaked, f"temp files were orphaned after failed download: {leaked}"

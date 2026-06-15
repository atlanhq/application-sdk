"""Unit tests for storage.reference using MemoryStore (no real I/O)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage.errors import StorageError, StorageNotFoundError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes, _put
from application_sdk.storage.reference import (
    _sha256_hex_file,
    _write_local_sidecar,
    materialize_file_reference,
    persist_file_reference,
)


@pytest.fixture
def store():
    return create_memory_store()


def _hash_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestSha256Helper:
    def test_streaming_sha256_matches_one_shot(self, tmp_path) -> None:
        f = tmp_path / "data.bin"
        content = b"some bytes here"
        f.write_bytes(content)
        assert _sha256_hex_file(f) == _hash_bytes(content)

    def test_empty_file(self, tmp_path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _sha256_hex_file(f) == hashlib.sha256(b"").hexdigest()


class TestWriteLocalSidecar:
    def test_writes_sidecar(self, tmp_path) -> None:
        local = tmp_path / "f.txt"
        local.write_bytes(b"x")
        _write_local_sidecar(str(local), "deadbeef")
        assert (tmp_path / "f.txt.sha256").read_text() == "deadbeef"

    def test_failure_swallowed(self, tmp_path) -> None:
        # Pointing at a directory that doesn't exist → write fails → swallowed
        bogus = str(tmp_path / "no_such_dir" / "x.txt")
        # Should NOT raise
        _write_local_sidecar(bogus, "abc")


# ---------------------------------------------------------------------------
# persist_file_reference
# ---------------------------------------------------------------------------


class TestPersistFileReference:
    async def test_already_durable_short_circuits(self, store) -> None:
        ref = FileReference(local_path="/tmp/x", is_durable=True, storage_path="k/x")
        result = await persist_file_reference(store, ref)
        assert result is ref

    async def test_local_path_none_raises_storage_error(self, store) -> None:
        """BLDX-1129 anchor: exercises function-local
        `from application_sdk.storage.errors import StorageError`."""
        ref = FileReference(local_path=None, is_durable=False)
        with pytest.raises(StorageError):
            await persist_file_reference(store, ref)

    async def test_single_file_upload_with_explicit_key(self, store, tmp_path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"payload")
        ref = FileReference(local_path=str(f))
        result = await persist_file_reference(store, ref, key="explicit/path.bin")
        assert result.is_durable is True
        assert result.storage_path == "explicit/path.bin"
        # Stored content roundtrips
        assert (
            await _get_bytes("explicit/path.bin", store, normalize=False) == b"payload"
        )
        # Stored sidecar present
        sidecar = await _get_bytes("explicit/path.bin.sha256", store, normalize=False)
        assert sidecar is not None
        assert sidecar.decode().strip() == _hash_bytes(b"payload")
        # Local sidecar created
        assert (tmp_path / "data.bin.sha256").read_text() == _hash_bytes(b"payload")

    async def test_single_file_generates_storage_path_when_no_key(
        self, store, tmp_path
    ) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"abc")
        ref = FileReference(local_path=str(f), tier=StorageTier.TRANSIENT)
        result = await persist_file_reference(store, ref)
        assert result.is_durable is True
        assert result.storage_path is not None
        # TRANSIENT tier → starts with file_refs/
        assert result.storage_path.startswith("file_refs/")
        assert result.storage_path.endswith(".txt")

    async def test_directory_upload(self, store, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        ref = FileReference(local_path=str(tmp_path), tier=StorageTier.TRANSIENT)

        result = await persist_file_reference(store, ref)
        assert result.is_durable is True
        assert result.file_count == 2
        # Verify each uploaded file plus its sidecar
        prefix = result.storage_path
        assert prefix is not None
        a_key = f"{prefix}sub/a.txt"
        b_key = f"{prefix}b.txt"
        assert await _get_bytes(a_key, store, normalize=False) == b"a"
        assert await _get_bytes(b_key, store, normalize=False) == b"b"
        # Sidecars
        a_side = await _get_bytes(a_key + ".sha256", store, normalize=False)
        assert a_side is not None
        assert a_side.decode().strip() == _hash_bytes(b"a")

    async def test_retained_tier_requires_run_prefix(self, store, tmp_path) -> None:
        from application_sdk.contracts.types_errors import RunPrefixRequiredError

        f = tmp_path / "data.bin"
        f.write_bytes(b"x")
        ref = FileReference(local_path=str(f), tier=StorageTier.RETAINED)
        with pytest.raises(RunPrefixRequiredError) as exc_info:
            await persist_file_reference(store, ref)
        assert exc_info.value.code == "INVALID_INPUT_RUN_PREFIX_REQUIRED"

    async def test_retained_tier_with_output_path(self, store, tmp_path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"x")
        ref = FileReference(local_path=str(f), tier=StorageTier.RETAINED)
        result = await persist_file_reference(store, ref, output_path="run/abc/123")
        assert result.is_durable is True
        assert result.storage_path is not None
        assert result.storage_path.startswith("run/abc/123/file_refs/")

    async def test_sidecar_write_failure_swallowed(self, store, tmp_path) -> None:
        """If _put for sidecar fails, persist still returns durable ref."""
        f = tmp_path / "data.bin"
        f.write_bytes(b"x")
        ref = FileReference(local_path=str(f))

        # Patch the inline-imported _put used inside persist_file_reference
        # to raise on sidecar puts (key endswith .sha256), pass-through otherwise.
        from application_sdk.storage import ops as ops_mod

        real_put = ops_mod._put

        async def flaky_put(key, data, st, **kwargs):
            if key.endswith(".sha256"):
                raise RuntimeError("sidecar fail")
            return await real_put(key, data, st, **kwargs)

        with patch.object(ops_mod, "_put", side_effect=flaky_put):
            result = await persist_file_reference(store, ref, key="some/key.bin")
        assert result.is_durable is True

    async def test_directory_sidecar_write_failure_swallowed(
        self, store, tmp_path
    ) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        ref = FileReference(local_path=str(tmp_path), tier=StorageTier.TRANSIENT)

        from application_sdk.storage import ops as ops_mod

        real_put = ops_mod._put

        async def flaky_put(key, data, st, **kwargs):
            if key.endswith(".sha256"):
                raise RuntimeError("sidecar fail")
            return await real_put(key, data, st, **kwargs)

        with patch.object(ops_mod, "_put", side_effect=flaky_put):
            result = await persist_file_reference(store, ref)
        assert result.is_durable is True
        assert result.file_count == 1


# ---------------------------------------------------------------------------
# materialize_file_reference
# ---------------------------------------------------------------------------


class TestMaterializeFileReference:
    async def test_non_durable_short_circuits(self, store) -> None:
        ref = FileReference(local_path="/tmp/x", is_durable=False)
        result = await materialize_file_reference(store, ref)
        assert result is ref

    async def test_no_storage_path_short_circuits(self, store) -> None:
        ref = FileReference(local_path="/tmp/x", is_durable=True, storage_path=None)
        result = await materialize_file_reference(store, ref)
        assert result is ref

    async def test_single_file_fast_path_with_matching_sidecar(
        self, store, tmp_path
    ) -> None:
        """File present locally + remote sidecar matches → no re-download."""
        # Upload a file via persist to set up store + sidecar.
        f = tmp_path / "data.bin"
        f.write_bytes(b"payload")
        # Stage file in store + remote sidecar
        await _put("k/data.bin", b"payload", store, normalize=False)
        await _put(
            "k/data.bin.sha256",
            _hash_bytes(b"payload").encode(),
            store,
            normalize=False,
        )
        ref = FileReference(
            local_path=str(f), is_durable=True, storage_path="k/data.bin"
        )
        result = await materialize_file_reference(store, ref)
        # Returned the same ref; local sidecar written.
        assert result is ref
        assert (tmp_path / "data.bin.sha256").read_text() == _hash_bytes(b"payload")

    async def test_single_file_hash_mismatch_redownloads(self, store, tmp_path) -> None:
        """Local file present, remote sidecar exists but mismatches → re-download."""
        await _put("k/data.bin", b"correct", store, normalize=False)
        await _put(
            "k/data.bin.sha256",
            _hash_bytes(b"correct").encode(),
            store,
            normalize=False,
        )
        # Local file has wrong content
        f = tmp_path / "data.bin"
        f.write_bytes(b"WRONG")
        ref = FileReference(
            local_path=str(f), is_durable=True, storage_path="k/data.bin"
        )
        result = await materialize_file_reference(store, ref)
        # local_path was rewritten with correct content
        assert Path(result.local_path).read_bytes() == b"correct"

    async def test_single_file_no_sidecar_redownloads(self, store, tmp_path) -> None:
        """Local file exists; no stored sidecar → conservative re-download path."""
        await _put("k/data.bin", b"server", store, normalize=False)
        f = tmp_path / "data.bin"
        f.write_bytes(b"local")
        ref = FileReference(
            local_path=str(f), is_durable=True, storage_path="k/data.bin"
        )
        result = await materialize_file_reference(store, ref)
        assert Path(result.local_path).read_bytes() == b"server"

    async def test_single_file_no_local_path_uses_tempfile(
        self, store, tmp_path
    ) -> None:
        await _put("k/file.bin", b"hello", store, normalize=False)
        await _put(
            "k/file.bin.sha256",
            _hash_bytes(b"hello").encode(),
            store,
            normalize=False,
        )
        ref = FileReference(local_path=None, is_durable=True, storage_path="k/file.bin")
        result = await materialize_file_reference(store, ref, local_dir=str(tmp_path))
        assert result.local_path is not None
        assert Path(result.local_path).read_bytes() == b"hello"
        assert result.local_path.endswith(".bin")

    async def test_single_file_no_local_path_no_local_dir(self, store) -> None:
        """Without local_path or local_dir, uses system tmp."""
        await _put("k/x.txt", b"sys-temp", store, normalize=False)
        await _put(
            "k/x.txt.sha256",
            _hash_bytes(b"sys-temp").encode(),
            store,
            normalize=False,
        )
        ref = FileReference(is_durable=True, storage_path="k/x.txt")
        result = await materialize_file_reference(store, ref)
        assert result.local_path is not None
        assert Path(result.local_path).read_bytes() == b"sys-temp"
        # Cleanup
        Path(result.local_path).unlink(missing_ok=True)
        Path(result.local_path + ".sha256").unlink(missing_ok=True)

    async def test_single_file_not_found_raises(self, store, tmp_path) -> None:
        """Storage path absent → StorageNotFoundError.

        BLDX-1129 anchor: exercises function-local imports of
        `download_file`, `StorageNotFoundError`, and `list_keys`.
        """
        ref = FileReference(
            local_path=str(tmp_path / "absent.bin"),
            is_durable=True,
            storage_path="missing/key.bin",
        )
        with pytest.raises(StorageNotFoundError):
            await materialize_file_reference(store, ref)

    async def test_single_file_sha256_mismatch_after_download_raises(
        self, store, tmp_path
    ) -> None:
        """If downloaded hash != stored sidecar hash → StorageError."""
        await _put("k/x.bin", b"server", store, normalize=False)
        # Stored sidecar lies about the hash
        await _put(
            "k/x.bin.sha256", b"deadbeef" * 8, store, normalize=False
        )  # wrong hash
        ref = FileReference(
            local_path=str(tmp_path / "out.bin"),
            is_durable=True,
            storage_path="k/x.bin",
        )
        with pytest.raises(StorageError):
            await materialize_file_reference(store, ref)

    async def test_directory_materialize(self, store, tmp_path) -> None:
        """Directory prefix → all files re-downloaded."""
        await _put("dirkey/a.txt", b"alpha", store, normalize=False)
        await _put("dirkey/sub/b.txt", b"beta", store, normalize=False)
        # Sidecars under the prefix should be ignored on listing
        await _put("dirkey/a.txt.sha256", b"x" * 64, store, normalize=False)

        local_dir = tmp_path / "out"
        ref = FileReference(
            local_path=str(local_dir),
            is_durable=True,
            storage_path="dirkey",
        )
        result = await materialize_file_reference(store, ref)
        assert result.file_count == 2
        assert (local_dir / "a.txt").read_bytes() == b"alpha"
        assert (local_dir / "sub" / "b.txt").read_bytes() == b"beta"

    async def test_directory_materialize_no_local_path_uses_local_dir(
        self, store, tmp_path
    ) -> None:
        await _put("dk/a.txt", b"alpha", store, normalize=False)
        ref = FileReference(local_path=None, is_durable=True, storage_path="dk")
        result = await materialize_file_reference(
            store, ref, local_dir=str(tmp_path / "ldir")
        )
        assert result.local_path == str(tmp_path / "ldir")
        assert (tmp_path / "ldir" / "a.txt").read_bytes() == b"alpha"

    async def test_directory_materialize_no_local_path_no_local_dir(
        self, store
    ) -> None:
        await _put("dk2/x.txt", b"data", store, normalize=False)
        ref = FileReference(local_path=None, is_durable=True, storage_path="dk2")
        result = await materialize_file_reference(store, ref)
        assert result.local_path is not None
        # Cleanup
        Path(result.local_path).joinpath("x.txt").unlink(missing_ok=True)

    async def test_directory_materialize_path_traversal_rejected(
        self, store, tmp_path
    ) -> None:
        """A listed key containing ``..`` must not write outside *local_path*.

        obstore rejects ``..`` keys on put, so we patch ``list_keys`` to plant
        a hostile listing and assert the containment guard fires before any
        write happens (issue #1694).
        """
        from unittest.mock import AsyncMock

        local_dir = tmp_path / "out"
        canary = tmp_path / "canary.txt"
        ref = FileReference(
            local_path=str(local_dir),
            is_durable=True,
            storage_path="dirkey",
        )
        with (
            patch(
                "application_sdk.storage.batch.list_keys",
                new=AsyncMock(return_value=["dirkey/../../canary.txt"]),
            ),
            pytest.raises(StorageError, match="Path traversal"),
        ):
            await materialize_file_reference(store, ref)
        assert not canary.exists()


# ---------------------------------------------------------------------------
# Sidecar fetch error handling (best-effort)
# ---------------------------------------------------------------------------


class TestStoredSidecarFailureSwallowed:
    async def test_download_returns_none_raises_not_found(
        self, store, tmp_path
    ) -> None:
        """If download_file returns None (no data), raise StorageNotFoundError.

        download_file is patched to simulate the rare 'returned None' branch —
        with MemoryStore it normally raises, never returns None.
        """
        # Stage list_keys such that the storage_path is treated as single-file
        # (i.e. no sub-keys). We have no objects under "phantom/key.bin/".
        from application_sdk.storage import ops as ops_mod

        async def fake_download(*args, **kwargs):
            return None

        ref = FileReference(
            local_path=str(tmp_path / "out.bin"),
            is_durable=True,
            storage_path="phantom/key.bin",
        )
        with patch.object(ops_mod, "download_file", side_effect=fake_download):
            with pytest.raises(StorageNotFoundError):
                await materialize_file_reference(store, ref)

    async def test_get_stored_sidecar_failure_falls_back_to_redownload(
        self, store, tmp_path
    ) -> None:
        """If sidecar fetch raises, materialize falls through (no crash)."""
        await _put("k/x.bin", b"actual", store, normalize=False)
        # Patch the function-local import inside reference.py
        f = tmp_path / "x.bin"
        f.write_bytes(b"actual")
        from application_sdk.storage import ops as ops_mod

        real_get_bytes = ops_mod._get_bytes

        async def flaky_get_bytes(key, st, **kwargs):
            if key.endswith(".sha256"):
                raise RuntimeError("transient sidecar failure")
            return await real_get_bytes(key, st, **kwargs)

        ref = FileReference(local_path=str(f), is_durable=True, storage_path="k/x.bin")
        with patch.object(ops_mod, "_get_bytes", side_effect=flaky_get_bytes):
            # Sidecar fetch fails → returns None → conservative re-download path.
            result = await materialize_file_reference(store, ref)
        assert Path(result.local_path).read_bytes() == b"actual"


# ---------------------------------------------------------------------------
# Directory materialize — per-file sidecar fast-path (BLDX-1155)
# ---------------------------------------------------------------------------


class TestDirectoryMaterializeSidecarFastPath:
    async def test_matching_sidecar_skips_download(self, store, tmp_path) -> None:
        """File whose local hash matches local sidecar is skipped entirely."""
        await _put("dir/a.txt", b"aaa", store, normalize=False)

        local_dir = tmp_path / "out"
        local_dir.mkdir()
        (local_dir / "a.txt").write_bytes(b"aaa")
        (local_dir / "a.txt.sha256").write_text(_hash_bytes(b"aaa"))

        ref = FileReference(
            local_path=str(local_dir), is_durable=True, storage_path="dir"
        )

        import application_sdk.storage.ops as ops_mod

        with patch.object(
            ops_mod, "download_file", wraps=ops_mod.download_file
        ) as mock_dl:
            result = await materialize_file_reference(store, ref)

        mock_dl.assert_not_awaited()
        assert result.file_count == 1
        assert (local_dir / "a.txt").read_bytes() == b"aaa"

    async def test_hash_mismatch_triggers_redownload(self, store, tmp_path) -> None:
        """Local file hash doesn't match local sidecar → falls through to re-download.

        The fast-path checks local_hash == local_sidecar_content.  When they
        disagree (corrupt local file or sidecar written for a different version),
        the file is re-downloaded from the store.
        """
        await _put("dir2/f.txt", b"server-version", store, normalize=False)

        local_dir = tmp_path / "out"
        local_dir.mkdir()
        (local_dir / "f.txt").write_bytes(b"stale-local")
        # Sidecar records a DIFFERENT hash than the actual local file has,
        # so local_hash != sidecar → fast-path falls through to re-download.
        (local_dir / "f.txt.sha256").write_text(_hash_bytes(b"server-version"))

        ref = FileReference(
            local_path=str(local_dir), is_durable=True, storage_path="dir2"
        )
        result = await materialize_file_reference(store, ref)

        assert (local_dir / "f.txt").read_bytes() == b"server-version"
        assert result.file_count == 1

    async def test_mixed_cached_and_new_files(self, store, tmp_path) -> None:
        """Cached files are skipped; only uncached files are downloaded."""
        await _put("dir3/cached.txt", b"cached", store, normalize=False)
        await _put("dir3/fresh.txt", b"fresh", store, normalize=False)

        local_dir = tmp_path / "out"
        local_dir.mkdir()
        # Pre-populate one file with a matching sidecar
        (local_dir / "cached.txt").write_bytes(b"cached")
        (local_dir / "cached.txt.sha256").write_text(_hash_bytes(b"cached"))
        # fresh.txt is absent locally

        ref = FileReference(
            local_path=str(local_dir), is_durable=True, storage_path="dir3"
        )

        import application_sdk.storage.ops as ops_mod

        download_calls: list[str] = []
        real_dl = ops_mod.download_file

        async def tracking_dl(key, *args, **kwargs):
            download_calls.append(key)
            return await real_dl(key, *args, **kwargs)

        with patch.object(ops_mod, "download_file", side_effect=tracking_dl):
            result = await materialize_file_reference(store, ref)

        # Only fresh.txt should have been downloaded
        assert len(download_calls) == 1
        assert download_calls[0].endswith("fresh.txt")
        assert result.file_count == 2
        assert (local_dir / "cached.txt").read_bytes() == b"cached"
        assert (local_dir / "fresh.txt").read_bytes() == b"fresh"

    async def test_all_cached_no_downloads(self, store, tmp_path) -> None:
        """When all files are cached with matching sidecars, zero downloads occur."""
        await _put("dir4/x.txt", b"xxx", store, normalize=False)
        await _put("dir4/y.txt", b"yyy", store, normalize=False)

        local_dir = tmp_path / "out"
        local_dir.mkdir()
        for name, content in [("x.txt", b"xxx"), ("y.txt", b"yyy")]:
            (local_dir / name).write_bytes(content)
            (local_dir / f"{name}.sha256").write_text(_hash_bytes(content))

        ref = FileReference(
            local_path=str(local_dir), is_durable=True, storage_path="dir4"
        )

        import application_sdk.storage.ops as ops_mod

        with patch.object(
            ops_mod, "download_file", wraps=ops_mod.download_file
        ) as mock_dl:
            result = await materialize_file_reference(store, ref)

        mock_dl.assert_not_awaited()
        assert result.file_count == 2

    async def test_nested_subdirectory_files_downloaded(self, store, tmp_path) -> None:
        """Files in subdirs within a prefix are placed at the correct local paths."""
        await _put("dir5/sub/deep.txt", b"deep", store, normalize=False)
        await _put("dir5/top.txt", b"top", store, normalize=False)

        local_dir = tmp_path / "out"
        ref = FileReference(
            local_path=str(local_dir), is_durable=True, storage_path="dir5"
        )
        result = await materialize_file_reference(store, ref)

        assert result.file_count == 2
        assert (local_dir / "top.txt").read_bytes() == b"top"
        assert (local_dir / "sub" / "deep.txt").read_bytes() == b"deep"


# ---------------------------------------------------------------------------
# Directory persist — concurrent upload (BLDX-1155)
# ---------------------------------------------------------------------------


class TestDirectoryPersistConcurrent:
    async def test_all_files_uploaded_with_sidecars(self, store, tmp_path) -> None:
        """Every file in the directory is uploaded with a remote .sha256 sidecar."""
        (tmp_path / "a.txt").write_bytes(b"aaa")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_bytes(b"bbb")

        ref = FileReference(local_path=str(tmp_path), tier=StorageTier.TRANSIENT)
        result = await persist_file_reference(store, ref)

        assert result.is_durable is True
        assert result.file_count == 2
        prefix = result.storage_path
        assert await _get_bytes(f"{prefix}a.txt", store, normalize=False) == b"aaa"
        assert await _get_bytes(f"{prefix}sub/b.txt", store, normalize=False) == b"bbb"
        sidecar = await _get_bytes(f"{prefix}a.txt.sha256", store, normalize=False)
        assert sidecar is not None
        assert sidecar.decode().strip() == _hash_bytes(b"aaa")

    async def test_concurrent_uploads_respect_semaphore(self, store, tmp_path) -> None:
        """Upload concurrency is bounded by MAX_CONCURRENT_STORAGE_TRANSFERS."""
        from unittest.mock import patch

        import application_sdk.storage.ops as ops_mod

        for i in range(6):
            (tmp_path / f"f{i}.bin").write_bytes(f"content{i}".encode())

        max_active = 0
        active = 0
        real_upload = ops_mod.upload_file

        async def tracking_upload(*args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                return await real_upload(*args, **kwargs)
            finally:
                active -= 1

        with patch.object(ops_mod, "upload_file", side_effect=tracking_upload):
            from application_sdk import constants

            with patch.object(constants, "MAX_CONCURRENT_STORAGE_TRANSFERS", 2):
                ref = FileReference(
                    local_path=str(tmp_path), tier=StorageTier.TRANSIENT
                )
                result = await persist_file_reference(store, ref)

        assert result.file_count == 6
        assert max_active <= 2, f"Expected max 2 concurrent uploads, got {max_active}"


class TestPersistFileReferenceListingRace:
    """Reproduces the rglob listing race observed in production.

    When ``Path.rglob("*")`` returns empty for a non-empty directory
    (cpython#146646 — pathlib silently swallows ``OSError`` mid-walk —
    and APFS directory-metadata visibility on macOS under concurrent
    load), ``persist_file_reference`` logs a successful completion
    with ``file_count=0`` and zero files actually uploaded. After
    migration to ``safe_list_directory``, ``os.scandir`` is used
    instead and all files are uploaded regardless of the rglob
    transient.
    """

    async def test_directory_upload_finds_files_when_rglob_returns_empty(
        self, store, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")

        ref = FileReference(local_path=str(tmp_path), tier=StorageTier.TRANSIENT)

        # Inject the listing race
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        result = await persist_file_reference(store, ref)

        # On main: file_count == 0 (the bug). After fix: 3.
        assert result.file_count == 3
        # And the bytes actually made it to storage
        prefix = result.storage_path
        assert prefix is not None
        assert await _get_bytes(f"{prefix}a.txt", store, normalize=False) == b"a"
        assert await _get_bytes(f"{prefix}b.txt", store, normalize=False) == b"b"
        assert await _get_bytes(f"{prefix}sub/c.txt", store, normalize=False) == b"c"

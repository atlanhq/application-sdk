"""Unit tests for storage.transfer upload/download with MemoryStore."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.contracts.storage import UploadOutput
from application_sdk.storage.batch import list_keys
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes
from application_sdk.storage.transfer import download, upload

_IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def store():
    return create_memory_store()


def _hash_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


# ``_sha256_file`` / ``_sha256_file_async`` moved to
# ``storage.integrity.sha256_file``; see ``test_integrity.py``.


class TestUploadSingleFile:
    async def test_upload_file_returns_durable_ref(self, store, tmp_path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"hello")
        out = await upload(str(f), store=store)
        assert isinstance(out, UploadOutput)
        assert out.ref.is_durable is True
        assert out.ref.local_path == str(f)
        assert out.ref.storage_path is not None
        assert out.ref.file_count == 1
        assert out.synced is True

    async def test_upload_file_skip_if_exists_same_hash(self, store, tmp_path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"hello")
        await upload(str(f), store=store, skip_if_exists=True)
        out2 = await upload(str(f), store=store, skip_if_exists=True)
        assert out2.synced is False
        assert out2.reason == "skipped:hash_match"

    async def test_upload_file_skip_if_exists_changed(self, store, tmp_path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"v1")
        await upload(str(f), store=store, skip_if_exists=True)
        f.write_bytes(b"v2")
        out2 = await upload(str(f), store=store, skip_if_exists=True)
        assert out2.synced is True

    async def test_upload_with_explicit_storage_path(self, store, tmp_path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"payload")
        out = await upload(str(f), "custom/key.txt", store=store)
        assert out.ref.storage_path == "custom/key.txt"

    async def test_upload_nonexistent_path_raises(self, store) -> None:
        from application_sdk.storage.errors import StorageError

        with pytest.raises(StorageError):
            await upload("/nonexistent/path.txt", store=store)


class TestUploadDirectory:
    async def test_upload_directory_returns_correct_file_count(
        self, store, tmp_path
    ) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")
        out = await upload(str(tmp_path), "myprefix", store=store)
        assert out.ref.file_count == 3
        assert out.ref.is_durable is True
        assert out.synced is True

    async def test_upload_directory_skip_unchanged(self, store, tmp_path) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        await upload(str(tmp_path), "myprefix", store=store, skip_if_exists=True)
        out2 = await upload(str(tmp_path), "myprefix", store=store, skip_if_exists=True)
        assert out2.synced is False
        assert out2.reason == "skipped:hash_match"

    async def test_upload_directory_concurrent_completes(self, store, tmp_path) -> None:
        """Multi-file directory upload completes correctly via concurrent path."""
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_bytes(f"content_{i}".encode())
        out = await upload(str(tmp_path), "conc", store=store)
        assert out.ref.file_count == 10
        assert out.synced is True
        assert out.reason == "uploaded"

        # Verify all files are downloadable
        dest = tmp_path / "dest"
        dl = await download("conc/", str(dest), store=store)
        assert dl.ref.file_count == 10

    async def test_upload_directory_partial_skip_count(self, store, tmp_path) -> None:
        """transferred_count is accurate when some files are skipped."""
        (tmp_path / "a.txt").write_bytes(b"aaa")
        (tmp_path / "b.txt").write_bytes(b"bbb")
        (tmp_path / "c.txt").write_bytes(b"ccc")

        # Upload once so all files get sidecars
        await upload(str(tmp_path), "partial", store=store, skip_if_exists=True)

        # Change only one file
        (tmp_path / "b.txt").write_bytes(b"bbb_v2")
        out = await upload(str(tmp_path), "partial", store=store, skip_if_exists=True)

        # Only the changed file should have been transferred
        assert out.synced is True
        assert out.reason == "uploaded"

    async def test_upload_directory_error_propagation(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """Error in one upload propagates correctly from asyncio.gather."""
        (tmp_path / "ok.txt").write_bytes(b"fine")
        (tmp_path / "fail.txt").write_bytes(b"boom")

        from application_sdk.storage import transfer as transfer_mod

        _original = transfer_mod._upload_one

        async def _failing_upload_one(st, local_file, store_key, **kwargs):
            if "fail.txt" in str(local_file):
                raise RuntimeError("simulated upload failure")
            return await _original(st, local_file, store_key, **kwargs)

        monkeypatch.setattr(transfer_mod, "_upload_one", _failing_upload_one)

        with pytest.raises(RuntimeError, match="simulated upload failure"):
            await upload(str(tmp_path), "errtest", store=store)


class TestUploadRaiseOnEmpty:
    """BLDX-1255: opt-in fail-loud when upload finds zero files.

    Default is ``raise_on_empty=False`` (preserve historical silent-zero
    behavior that incremental extractors rely on). Connectors hit by
    silent-failure incidents (Tableau / Looker / Coalesce / dbt) opt in by
    passing ``raise_on_empty=True``.
    """

    async def test_empty_dir_with_raise_on_empty_true_raises(
        self, store, tmp_path
    ) -> None:
        from application_sdk.storage.errors import StorageEmptyUploadError

        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(StorageEmptyUploadError, match="contains zero files"):
            await upload(str(empty), "myprefix", store=store, raise_on_empty=True)

    async def test_empty_dir_with_raise_on_empty_false_returns_zero_count(
        self, store, tmp_path
    ) -> None:
        """Regression pin: default behavior (silent zero) preserved when opt-in not set.

        Incremental extractors that legitimately have quiet-day runs (no
        new data since last watermark) rely on this. Flipping this would
        break ~19 production connectors — see BLDX-1255 audit.
        """
        empty = tmp_path / "empty"
        empty.mkdir()

        out = await upload(str(empty), "myprefix", store=store)
        assert out.ref.file_count == 0
        assert out.synced is False

    async def test_non_empty_dir_with_raise_on_empty_true_succeeds(
        self, store, tmp_path
    ) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")

        out = await upload(str(tmp_path), "myprefix", store=store, raise_on_empty=True)
        assert out.ref.file_count == 2
        assert out.synced is True


class TestUploadStorageSubdir:
    """Tests for the storage_subdir parameter on upload."""

    async def test_file_with_storage_subdir_and_app_prefix(
        self, store, tmp_path
    ) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"hello")
        out = await upload(
            str(f), store=store, _app_prefix="run/123", storage_subdir="dbt"
        )
        assert out.ref.storage_path == "run/123/dbt/data.txt"

    async def test_dir_with_storage_subdir_and_app_prefix(
        self, store, tmp_path
    ) -> None:
        d = tmp_path / "dbt"
        d.mkdir()
        (d / "models.json").write_bytes(b"m")
        (d / "tests.json").write_bytes(b"t")
        out = await upload(
            str(d), store=store, _app_prefix="run/123", storage_subdir="dbt"
        )
        assert out.ref.storage_path == "run/123/dbt/"
        assert out.ref.file_count == 2

    async def test_storage_path_overrides_storage_subdir(self, store, tmp_path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"payload")
        out = await upload(
            str(f), "explicit/key.txt", store=store, storage_subdir="ignored"
        )
        assert out.ref.storage_path == "explicit/key.txt"

    async def test_storage_subdir_without_app_prefix_is_ignored(
        self, store, tmp_path
    ) -> None:
        """storage_subdir only applies when _app_prefix is set."""
        d = tmp_path / "mydir"
        d.mkdir()
        (d / "a.txt").write_bytes(b"a")
        out = await upload(str(d), store=store, storage_subdir="dbt")
        # No _app_prefix → falls through to src.name, storage_subdir ignored
        assert out.ref.storage_path == "mydir/"

    async def test_storage_subdir_path_traversal_rejected(
        self, store, tmp_path
    ) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        f = tmp_path / "data.txt"
        f.write_bytes(b"x")
        with pytest.raises(UnsafeUploadPathError) as exc_info:
            await upload(
                str(f), store=store, _app_prefix="run/123", storage_subdir="../../etc"
            )
        assert exc_info.value.code == "INVALID_INPUT_UPLOAD_PATH_UNSAFE"


class TestUploadSensitivePathBlocking:
    """Tests for blocking uploads from sensitive system paths."""

    @pytest.mark.skipif(_IS_WINDOWS, reason="Unix-only sensitive paths")
    async def test_etc_blocked(self, store) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        with pytest.raises(UnsafeUploadPathError):
            await upload("/etc/passwd", store=store)

    @pytest.mark.skipif(_IS_WINDOWS, reason="Unix-only sensitive paths")
    async def test_proc_blocked(self, store) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        with pytest.raises(UnsafeUploadPathError):
            await upload("/proc/self/environ", store=store)

    async def test_aws_dir_blocked(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir()
        creds = aws_dir / "credentials"
        creds.write_bytes(b"secret")
        with pytest.raises(UnsafeUploadPathError):
            await upload(str(creds), store=store)

    async def test_ssh_dir_blocked(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_rsa"
        key.write_bytes(b"private-key")
        with pytest.raises(UnsafeUploadPathError):
            await upload(str(key), store=store)

    async def test_env_file_blocked(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        env_file = tmp_path / ".env"
        env_file.write_bytes(b"SECRET=value")
        with pytest.raises(UnsafeUploadPathError):
            await upload(str(env_file), store=store)

    async def test_env_local_file_blocked(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        env_file = tmp_path / ".env.local"
        env_file.write_bytes(b"SECRET=value")
        with pytest.raises(UnsafeUploadPathError):
            await upload(str(env_file), store=store)

    async def test_path_traversal_blocked(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import UnsafeUploadPathError

        with pytest.raises(UnsafeUploadPathError):
            await upload(str(tmp_path / ".." / "etc" / "passwd"), store=store)

    async def test_normal_path_allowed(self, store, tmp_path) -> None:
        f = tmp_path / "normal.txt"
        f.write_bytes(b"safe content")
        out = await upload(str(f), store=store)
        assert out.ref.is_durable is True

    async def test_user_blocked_paths_env_var(
        self, store, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATLAN_UPLOAD_FILE_BLOCKED_PATHS", "/custom/secrets/,.vault")
        f = tmp_path / "normal.txt"
        f.write_bytes(b"safe")
        # Normal path should still work
        out = await upload(str(f), store=store)
        assert out.ref.is_durable is True

    async def test_user_blocked_paths_matches(
        self, store, tmp_path, monkeypatch
    ) -> None:
        custom_dir = tmp_path / "custom_secrets"
        custom_dir.mkdir()
        secret = custom_dir / "token"
        secret.write_bytes(b"secret")
        monkeypatch.setenv(
            "ATLAN_UPLOAD_FILE_BLOCKED_PATHS", "custom_secrets,.credentials"
        )
        from application_sdk.storage.errors import UnsafeUploadPathError

        with pytest.raises(UnsafeUploadPathError):
            await upload(str(secret), store=store)


class TestDownloadSingleFile:
    async def test_roundtrip_single_file(self, store, tmp_path) -> None:
        f = tmp_path / "src.txt"
        f.write_bytes(b"roundtrip")
        await upload(str(f), "rt/src.txt", store=store)

        dest = tmp_path / "dest.txt"
        dl = await download("rt/src.txt", str(dest), store=store)
        assert dl.ref.local_path == str(dest)
        assert dl.ref.storage_path == "rt/src.txt"
        assert dl.ref.file_count == 1
        assert dest.read_bytes() == b"roundtrip"
        assert dl.synced is True

    async def test_download_skip_if_exists_same_hash(self, store, tmp_path) -> None:
        f = tmp_path / "src.txt"
        f.write_bytes(b"hello")
        await upload(str(f), "sk/src.txt", store=store)

        dest = tmp_path / "dest.txt"
        await download("sk/src.txt", str(dest), store=store)
        dl2 = await download("sk/src.txt", str(dest), store=store, skip_if_exists=True)
        assert dl2.synced is False
        assert dl2.reason == "skipped:hash_match"

    async def test_download_missing_key_raises(self, store, tmp_path) -> None:
        from application_sdk.storage.errors import StorageNotFoundError

        with pytest.raises(StorageNotFoundError):
            await download("no/such/key.txt", str(tmp_path / "out.txt"), store=store)


class TestDownloadDirectory:
    async def test_roundtrip_directory(self, store, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"a")
        (src / "b.txt").write_bytes(b"b")
        await upload(str(src), "dirtest/", store=store)

        dest = tmp_path / "dest"
        dl = await download("dirtest/", str(dest), store=store)
        assert dl.ref.file_count == 2
        assert (dest / "a.txt").read_bytes() == b"a"
        assert (dest / "b.txt").read_bytes() == b"b"

    async def test_sidecar_files_excluded_from_file_count(
        self, store, tmp_path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "data.txt").write_bytes(b"data")
        await upload(str(src), "sc/", store=store)

        dest = tmp_path / "dest"
        dl = await download("sc/", str(dest), store=store)
        # Only 1 real file — sidecar should not appear in file_count or on disk
        assert dl.ref.file_count == 1
        assert not (dest / "data.txt.sha256").exists()

    async def test_path_traversal_in_listed_key_rejected(self, store, tmp_path) -> None:
        """A listed key containing ``..`` must not write outside dest_dir.

        obstore rejects ``..`` keys on put, so we patch ``list_keys_with_meta``
        to plant a hostile listing and assert the containment guard fires before
        any write happens (issue #1694).
        """
        from unittest.mock import AsyncMock, patch

        from application_sdk.storage.errors import StorageError

        dest = tmp_path / "dest"
        canary = tmp_path / "canary.txt"
        # Trailing slash in storage_path puts download() straight into prefix
        # mode, so only the prefix listing is consulted.
        with (
            patch(
                "application_sdk.storage.batch.list_keys_with_meta",
                new=AsyncMock(return_value=[("p/safe/../../canary.txt", 10, None)]),
            ),
            pytest.raises(StorageError, match="Path traversal"),
        ):
            await download("p/", str(dest), store=store)
        assert not canary.exists()


class TestUploadDirectoryListingRace:
    """Inject the rglob listing transient (cpython#146646) and assert
    ``upload`` still returns the correct file_count. Mocking
    ``Path.rglob`` to return empty/partial proves the upload path is
    independent of pathlib's silent-swallow bug.
    """

    async def test_upload_finds_files_when_rglob_returns_empty(
        self, store, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")

        # Inject the listing race: Path.rglob returns empty even though
        # the directory has 3 files.
        # Regression guard: a future revert to Path.rglob would re-trigger this mock.
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        out = await upload(str(tmp_path), "race_prefix", store=store)

        # On main: file_count==0 (production silent-failure mode).
        # After fix: safe_list_directory bypasses rglob via os.scandir.
        assert out.ref.file_count == 3
        assert out.synced is True

    async def test_upload_finds_all_files_when_rglob_returns_partial(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """Partial-result variant: rglob silently truncates after a
        mid-walk OSError. The caller would see an undercount."""
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        (tmp_path / "c.txt").write_bytes(b"c")

        # Return only 1 of the 3 files — simulating partial-truncation.
        # Regression guard: a future revert to Path.rglob would re-trigger this mock.
        partial = [tmp_path / "a.txt"]
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter(partial))

        out = await upload(str(tmp_path), "partial_race", store=store)

        # On main: file_count==1 (silent undercount).
        # After fix: file_count==3 (all found via os.scandir).
        assert out.ref.file_count == 3
        assert out.synced is True

    async def test_upload_with_raise_on_empty_unaffected_by_rglob_transient(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """raise_on_empty=True must not misfire on a transient empty
        rglob when the directory actually has files."""
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")

        # Regression guard: a future revert to Path.rglob would re-trigger this mock.
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        out = await upload(
            str(tmp_path), "race_prefix", store=store, raise_on_empty=True
        )

        assert out.ref.file_count == 2


class TestUploadDirectorySourceStoreReconcile:
    """BLDX-1554: a *partially*-present local directory must be reconciled
    against the source (deployment) store so the target (upstream) copy is
    complete — the SDR cross-pod hand-off guarantee.

    Scenario: the parallel ``transform_*`` activities that populate
    ``transformed/`` are placed on different worker pods. The pod that runs the
    final ``App.upload`` holds only the entity types it happened to transform;
    the rest live only in the deployment store (persisted per-pod by the
    activity interceptor). Uploading local-only would drop whole types.
    """

    async def _seed(self, store, key: str, data: bytes) -> None:
        from application_sdk.storage.ops import _put

        await _put(key, data, store, normalize=False)

    async def _target_keys(self, store) -> set[str]:
        from application_sdk.storage.batch import list_data_keys

        return set(await list_data_keys("", store, normalize=False))

    async def _read(self, store, key: str) -> bytes | None:
        from application_sdk.storage.ops import _get_bytes

        return await _get_bytes(key, store, normalize=False)

    async def test_partial_local_dir_reconciled_from_source(self, tmp_path) -> None:
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()  # deployment store — has ALL types
        target = create_memory_store()  # upstream store — what publish reads
        src_prefix = "artifacts/apps/mysql/wf/run/transformed"
        await self._seed(source, f"{src_prefix}/table/entities.json", b"TABLES")
        # Source holds a STALE copy of the local-present file; local must win the
        # ``if rel in local_rels: continue`` skip (local is authoritative).
        await self._seed(source, f"{src_prefix}/column/entities.json", b"COLUMNS_STALE")

        # This pod only ran transform_columns → local dir holds ONLY column/.
        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"COLUMNS_FRESH")

        out = await upload(
            str(local),
            storage_path="dest/transformed",
            store=target,
            _source_ref=FileReference(local_path=str(local), storage_path=src_prefix),
            _source_store=source,
        )

        keys = await self._target_keys(target)
        assert "dest/transformed/column/entities.json" in keys  # uploaded from local
        assert "dest/transformed/table/entities.json" in keys  # streamed from source
        assert out.ref.file_count == 2
        # The local copy wins the collision — the stale source copy is not streamed.
        assert (
            await self._read(target, "dest/transformed/column/entities.json")
            == b"COLUMNS_FRESH"
        )

    async def test_union_local_only_and_source_only(self, tmp_path) -> None:
        """Union semantics: a local-only file (no source copy, e.g. a
        stream-writing connector) still uploads, and a source-only file is
        streamed. Neither is dropped."""
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()
        target = create_memory_store()
        src_prefix = "pfx/transformed"
        await self._seed(
            source, f"{src_prefix}/table/entities.json", b"T"
        )  # source-only

        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")  # local-only

        out = await upload(
            str(local),
            storage_path="d/transformed",
            store=target,
            _source_ref=FileReference(local_path=str(local), storage_path=src_prefix),
            _source_store=source,
        )

        keys = await self._target_keys(target)
        assert "d/transformed/column/entities.json" in keys  # local-only preserved
        assert "d/transformed/table/entities.json" in keys  # source-only streamed
        # file_count = 1 local + 1 reconciled from source (disjoint union).
        assert out.ref.file_count == 2

    async def test_no_source_store_uploads_local_only(self, tmp_path) -> None:
        """Non-SDR (no source store): behaviour is unchanged — only local files
        are uploaded and no source-store lookup happens."""
        target = create_memory_store()
        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        await upload(str(local), storage_path="d/transformed", store=target)

        keys = await self._target_keys(target)
        assert keys == {"d/transformed/column/entities.json"}

    async def test_empty_local_dir_reconciles_without_raising(self, tmp_path) -> None:
        """An empty local dir with a populated source store must NOT trip
        raise_on_empty — the union is non-empty, and files stream from source."""
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()
        target = create_memory_store()
        src_prefix = "pfx/transformed"
        await self._seed(source, f"{src_prefix}/table/entities.json", b"T")

        local = tmp_path / "transformed"
        local.mkdir()  # exists but empty (this pod ran no transform)

        out = await upload(
            str(local),
            storage_path="d/transformed",
            store=target,
            raise_on_empty=True,
            _source_ref=FileReference(local_path=str(local), storage_path=src_prefix),
            _source_store=source,
        )

        keys = await self._target_keys(target)
        assert "d/transformed/table/entities.json" in keys
        # 0 local + 1 reconciled from source.
        assert out.ref.file_count == 1

    async def test_empty_local_and_empty_source_raises(self, tmp_path) -> None:
        """raise_on_empty still fires when both local and source are empty."""
        from application_sdk.contracts.types import FileReference
        from application_sdk.storage.errors import StorageEmptyUploadError

        source = create_memory_store()
        target = create_memory_store()
        local = tmp_path / "transformed"
        local.mkdir()

        with pytest.raises(StorageEmptyUploadError):
            await upload(
                str(local),
                storage_path="d/transformed",
                store=target,
                raise_on_empty=True,
                _source_ref=FileReference(
                    local_path=str(local), storage_path="pfx/transformed"
                ),
                _source_store=source,
            )

    async def test_same_store_identity_skips_reconcile(self, tmp_path) -> None:
        """When the source store IS the target store (non-SDR: the guard
        ``source_resolved is not resolved`` is false), reconciliation is skipped
        entirely — only local files land and the source store is never listed."""
        from application_sdk.contracts.types import FileReference

        store = create_memory_store()
        src_prefix = "pfx/transformed"
        # A source-only file sits in the same store; it must NOT be copied to the
        # target prefix because reconcile does not run.
        await self._seed(store, f"{src_prefix}/table/entities.json", b"T")

        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        # Patch the reconcile branch's actual call site (``transfer.list_data_keys``,
        # invoked via ``_list_source_data_keys``) rather than the lower-level
        # ``batch.list_keys`` it delegates to — a direct guard that survives an
        # inlining refactor of ``list_data_keys``.
        with patch("application_sdk.storage.transfer.list_data_keys") as spy:
            await upload(
                str(local),
                storage_path="d/transformed",
                store=store,
                _source_ref=FileReference(
                    local_path=str(local), storage_path=src_prefix
                ),
                _source_store=store,  # same object → identity guard trips
            )

        keys = await self._target_keys(store)
        # Local file landed under the target prefix; source-only file was NOT
        # reconciled into it (it still exists only at its original src_prefix).
        assert "d/transformed/column/entities.json" in keys
        assert "d/transformed/table/entities.json" not in keys
        # The reconcile branch's source-store LIST never fired.
        spy.assert_not_called()

    async def test_source_ref_path_traversal_blocked(self, tmp_path) -> None:
        """The reconcile branch applies the same ``..`` traversal guard on
        ``_source_ref.storage_path`` as the local-absent fallback."""
        from application_sdk.contracts.types import FileReference
        from application_sdk.storage.errors import UnsafeUploadPathError

        source = create_memory_store()
        target = create_memory_store()

        # Non-empty local dir → we reach the reconcile block (distinct source).
        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        with pytest.raises(UnsafeUploadPathError):
            await upload(
                str(local),
                storage_path="d/transformed",
                store=target,
                _source_ref=FileReference(
                    local_path=str(local), storage_path="pfx/../etc"
                ),
                _source_store=source,
            )

    async def test_source_sidecar_keys_skipped(self, tmp_path) -> None:
        """A SHA-256 sidecar key in the source store is not treated as a data
        file during reconcile: it is neither counted nor streamed as its own
        object (which would land a ``.sha256.sha256`` double-sidecar)."""
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()
        target = create_memory_store()
        src_prefix = "pfx/transformed"
        await self._seed(source, f"{src_prefix}/table/entities.json", b"T")
        # A sidecar next to the data file — must be excluded from reconcile.
        # Real digest of the data: the reconcile streams the object through the
        # transfer layer, which now verifies it against this sidecar (FND-306).
        await self._seed(
            source,
            f"{src_prefix}/table/entities.json.sha256",
            _hash_bytes(b"T").encode(),
        )

        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        out = await upload(
            str(local),
            storage_path="d/transformed",
            store=target,
            _source_ref=FileReference(local_path=str(local), storage_path=src_prefix),
            _source_store=source,
        )

        # Only the 1 local + 1 source *data* file are counted; the source sidecar
        # was filtered out, not counted as a third streamed file.
        assert out.ref.file_count == 2

        from application_sdk.storage.batch import list_keys

        all_target_keys = await list_keys("", target, normalize=False)
        assert "d/transformed/table/entities.json" in all_target_keys
        # The source sidecar was not streamed as data, so no double-sidecar lands.
        assert "d/transformed/table/entities.json.sha256.sha256" not in all_target_keys

    async def test_absent_local_dir_streams_complete_set_from_source(
        self, tmp_path
    ) -> None:
        """Local path *entirely* absent (the upload pod ran none of the
        transforms): every file is streamed from the source store. Guards the
        local-absent fallback branch that the partial-dir reconcile sits beside."""
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()
        target = create_memory_store()
        src_prefix = "pfx/transformed"
        await self._seed(source, f"{src_prefix}/table/entities.json", b"T")
        await self._seed(source, f"{src_prefix}/column/entities.json", b"C")

        missing_local = str(tmp_path / "never-created" / "transformed")

        out = await upload(
            missing_local,
            storage_path="d/transformed",
            store=target,
            _source_ref=FileReference(
                local_path=missing_local, storage_path=src_prefix
            ),
            _source_store=source,
        )

        keys = await self._target_keys(target)
        assert "d/transformed/table/entities.json" in keys
        assert "d/transformed/column/entities.json" in keys
        # Both source files are streamed and counted (distinct code path from the
        # partial-dir reconcile — assert the count as every sibling test does).
        assert out.ref.file_count == 2

    async def test_empty_source_storage_path_skips_reconcile(self, tmp_path) -> None:
        """When ``_source_ref`` carries an empty ``storage_path`` the reconcile
        guard (``and _source_ref.storage_path``) is false, so reconciliation is
        skipped even though a distinct source store is supplied: only local files
        land and the source store is never listed."""
        from application_sdk.contracts.types import FileReference

        source = create_memory_store()  # distinct store, but never consulted
        target = create_memory_store()
        # A source-only file that must NOT be streamed because reconcile is skipped.
        await self._seed(source, "pfx/transformed/table/entities.json", b"T")

        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        with patch("application_sdk.storage.transfer.list_data_keys") as spy:
            out = await upload(
                str(local),
                storage_path="d/transformed",
                store=target,
                _source_ref=FileReference(local_path=str(local), storage_path=""),
                _source_store=source,
            )

        keys = await self._target_keys(target)
        assert "d/transformed/column/entities.json" in keys  # local landed
        assert "d/transformed/table/entities.json" not in keys  # not reconciled
        assert out.ref.file_count == 1
        # Empty source prefix → the reconcile source-store LIST never fired.
        spy.assert_not_called()


def test_reconcile_prefix_alignment_holds_across_helpers() -> None:
    """Regression guard for the load-bearing assumption behind the directory
    reconcile: the source prefix the reconcile lists under
    (``normalize_key(local_dir)``, as ``App.upload`` derives ``_source_ref``)
    must match the keys the activity interceptor persists each transform output
    at (``get_object_store_prefix(local_dir/<type>/entities.json)``). If these
    two helpers ever diverge for ``TEMPORARY_PATH`` paths, the reconcile would
    silently find nothing and the SDR hand-off would regress — while the
    store-level tests above (self-consistent prefixes) stay green.
    """
    import os

    from application_sdk.constants import TEMPORARY_PATH
    from application_sdk.execution._temporal.activity_utils import (
        get_object_store_prefix,
    )
    from application_sdk.storage.ops import normalize_key

    base = os.path.join(
        TEMPORARY_PATH, "artifacts/apps/x/workflows/wf-1/run-1/transformed"
    )
    source_dir_prefix = normalize_key(base).rstrip("/") + "/"
    for typename in ("database", "schema", "table", "column"):
        persisted = get_object_store_prefix(
            os.path.join(base, typename, "entities.json")
        )
        assert persisted.startswith(
            source_dir_prefix
        ), f"{persisted!r} not under {source_dir_prefix!r}"
        assert persisted.removeprefix(source_dir_prefix) == f"{typename}/entities.json"


class TestUploadSameStoreCopy:
    """FND-536: a deployment→deployment copy must be expressible.

    ``App.upload`` hands every dual-write leg the deployment store as its
    fallback source, so the deployment leg of an ADR-0014 dual write reaches the
    local-absent fallback branch instead of raising "local_path does not exist".
    Two shapes matter:

    * keys pinned to the source prefix (the P042 bridge shape) — every copy is
      an object onto itself, so it must short-circuit without moving bytes and
      without the sidecar GETs the cross-store dedup would cost;
    * keys not pinned — a real copy inside the one store, which is what keeps
      ADR-0014's "identical key in both stores" promise for the ref-only case.
    """

    async def _seed(self, store, key: str, data: bytes) -> None:
        from application_sdk.storage.ops import _put

        await _put(key, data, store, normalize=False)

    async def _data_keys(self, store) -> set[str]:
        from application_sdk.storage.batch import list_data_keys

        return set(await list_data_keys("", store, normalize=False))

    async def _read(self, store, key: str) -> bytes | None:
        from application_sdk.storage.ops import _get_bytes

        return await _get_bytes(key, store, normalize=False)

    async def test_key_preserving_dir_copy_is_a_noop(self, tmp_path) -> None:
        """Local absent, source store IS the target store, and the destination
        prefix is pinned to the source prefix: every key is its own destination.
        No bytes move, no sidecar is read, and the upload still reports the full
        file count so the caller sees the prefix as satisfied."""
        from application_sdk.contracts.types import FileReference

        store = create_memory_store()
        prefix = "artifacts/apps/postgres/wf/run/transformed"
        await self._seed(store, f"{prefix}/table/entities.json", b"T")
        await self._seed(store, f"{prefix}/column/entities.json", b"C")

        missing_local = str(tmp_path / "never-created" / "transformed")

        # Patch the dedup helper, not the store: proves the same-object guard
        # returns *before* the two sidecar GETs, which is the whole point of
        # placing it ahead of the SHA-256 check.
        with patch(
            "application_sdk.storage.transfer._cross_store_sha256_match"
        ) as dedup_spy:
            out = await upload(
                missing_local,
                storage_path=prefix,
                store=store,
                _source_ref=FileReference(
                    local_path=missing_local, storage_path=prefix
                ),
                _source_store=store,
            )

        dedup_spy.assert_not_called()
        assert out.ref.file_count == 2
        assert out.synced == 0  # nothing transferred
        # The source objects are untouched — not deleted, not rewritten.
        assert await self._data_keys(store) == {
            f"{prefix}/table/entities.json",
            f"{prefix}/column/entities.json",
        }

    async def test_unpinned_dir_copy_moves_bytes_within_the_store(
        self, tmp_path
    ) -> None:
        """Same store, but the destination prefix differs from the source's: a
        real copy runs, so the one store ends up holding the artifacts under the
        canonical run prefix as well as the ref prefix."""
        from application_sdk.contracts.types import FileReference

        store = create_memory_store()
        src_prefix = "artifacts/apps/postgres/wf/run/transformed"
        payload = b'{"typeName": "Table", "attributes": {"name": "orders"}}\n'
        await self._seed(store, f"{src_prefix}/table/entities.json", payload)

        missing_local = str(tmp_path / "never-created" / "transformed")

        out = await upload(
            missing_local,
            storage_path="dest/transformed",
            store=store,
            _source_ref=FileReference(
                local_path=missing_local, storage_path=src_prefix
            ),
            _source_store=store,
        )

        assert out.ref.file_count == 1
        assert out.synced == 1
        assert await self._data_keys(store) == {
            f"{src_prefix}/table/entities.json",  # source retained
            "dest/transformed/table/entities.json",  # copy landed
        }
        # Read the bytes back: the right key holding truncated or altered
        # content would satisfy a key-only assertion.
        assert (
            await self._read(store, "dest/transformed/table/entities.json") == payload
        )
        assert await self._read(store, f"{src_prefix}/table/entities.json") == payload

    async def test_key_preserving_single_file_copy_is_a_noop(self, tmp_path) -> None:
        """Single-file fallback shape of the same guard, and the reason string
        says *why* it skipped — no hash was compared."""
        from application_sdk.contracts.types import FileReference

        store = create_memory_store()
        key = "artifacts/apps/postgres/wf/run/transformed/entities.json"
        await self._seed(store, key, b"T")

        missing_local = str(tmp_path / "never-created" / "entities.json")

        with patch(
            "application_sdk.storage.transfer._cross_store_sha256_match"
        ) as dedup_spy:
            out = await upload(
                missing_local,
                storage_path=key,
                store=store,
                _source_ref=FileReference(local_path=missing_local, storage_path=key),
                _source_store=store,
            )

        dedup_spy.assert_not_called()
        assert out.ref.file_count == 1
        assert out.synced == 0
        assert out.reason == "skipped:same_object"

    async def test_same_key_with_absent_source_object_still_raises(
        self, tmp_path
    ) -> None:
        """ "Satisfied" must mean the object is there. A stale ``FileReference``
        pinned to a key that was never written must not buy a durable-looking
        success out of the same-object guard — it fails with the same not-found
        error any other leg would raise."""
        from application_sdk.contracts.types import FileReference
        from application_sdk.storage.errors import StorageNotFoundError

        store = create_memory_store()  # nothing seeded — the key does not exist
        key = "artifacts/apps/postgres/wf/run/transformed/entities.json"
        missing_local = str(tmp_path / "never-created" / "entities.json")

        with pytest.raises(StorageNotFoundError):
            await upload(
                missing_local,
                storage_path=key,
                store=store,
                _source_ref=FileReference(local_path=missing_local, storage_path=key),
                _source_store=store,
            )

    async def test_listed_source_keys_skip_the_existence_head(self, tmp_path) -> None:
        """Keys enumerated from the source store are already proven present, so
        the directory fallback must not pay a HEAD per key to re-establish it."""
        from application_sdk.contracts.types import FileReference

        store = create_memory_store()
        prefix = "artifacts/apps/postgres/wf/run/transformed"
        await self._seed(store, f"{prefix}/table/entities.json", b"T")
        await self._seed(store, f"{prefix}/column/entities.json", b"C")

        missing_local = str(tmp_path / "never-created" / "transformed")

        with patch(
            "application_sdk.storage.ops.exists", side_effect=AssertionError("HEAD")
        ) as exists_spy:
            out = await upload(
                missing_local,
                storage_path=prefix,
                store=store,
                _source_ref=FileReference(
                    local_path=missing_local, storage_path=prefix
                ),
                _source_store=store,
            )

        exists_spy.assert_not_called()
        assert out.ref.file_count == 2
        assert out.synced == 0


# =============================================================================
# FND-1339: a directory upload is round-trip-bound — spend listings, not HEADs
# =============================================================================


def _make_tree(root: Path, n: int, size: int = 64) -> list[Path]:
    paths: list[Path] = []
    for i in range(n):
        d = root / "raw" / f"t{i % 3}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"chunk-{i}.parquet"
        p.write_bytes(bytes([i % 251]) * size)
        paths.append(p)
    return paths


class _RequestLog:
    """Records the per-object requests the directory path is meant to avoid.

    ``heads`` covers both HEAD shapes the old protocol spent per file (the
    sidecar-existence probe of the skip check and the readback), ``listings``
    the prefix listings that replace them, ``puts`` the data-object uploads.
    """

    def __init__(self, monkeypatch) -> None:
        import application_sdk.storage.ops as ops_mod
        from application_sdk.storage import transfer as transfer_mod

        self.heads: list[str] = []
        self.listings: list[str] = []
        self.puts: list[str] = []
        real_exists, real_meta = ops_mod.exists, ops_mod.get_file_meta
        real_list = transfer_mod.list_keys_with_meta
        real_upload = ops_mod.upload_file

        async def exists(key, *a, **kw):
            self.heads.append(key)
            return await real_exists(key, *a, **kw)

        async def meta(key, *a, **kw):
            self.heads.append(key)
            return await real_meta(key, *a, **kw)

        async def listing(prefix, *a, **kw):
            self.listings.append(prefix)
            return await real_list(prefix, *a, **kw)

        async def upload_file(key, *a, **kw):
            self.puts.append(key)
            return await real_upload(key, *a, **kw)

        monkeypatch.setattr(ops_mod, "exists", exists)
        monkeypatch.setattr(ops_mod, "get_file_meta", meta)
        monkeypatch.setattr(transfer_mod, "list_keys_with_meta", listing)
        monkeypatch.setattr(ops_mod, "upload_file", upload_file)


class TestUploadDirectoryRoundTrips:
    """Two listings per directory in place of two HEADs per file."""

    async def test_first_upload_lists_twice_and_never_heads(
        self, store, tmp_path, monkeypatch
    ) -> None:
        paths = _make_tree(tmp_path, 12)
        log = _RequestLog(monkeypatch)

        out = await upload(str(tmp_path), "runs/r1", store=store, skip_if_exists=True)

        assert out.ref.file_count == 12
        assert out.synced is True
        assert out.reason == "uploaded"
        assert log.heads == []
        # One listing answers the skip check, one is the readback.
        assert log.listings == ["runs/r1/", "runs/r1/"]
        assert len(log.puts) == 12
        keys = set(await list_keys("runs/r1/", store, normalize=False))
        for p in paths:
            key = f"runs/r1/{p.relative_to(tmp_path).as_posix()}"
            assert key in keys
            assert f"{key}.sha256" in keys
            sidecar = await _get_bytes(f"{key}.sha256", store, normalize=False)
            assert sidecar is not None
            assert sidecar.decode().strip() == _hash_bytes(p.read_bytes())

    async def test_retry_skips_every_file_with_one_listing_and_no_puts(
        self, store, tmp_path, monkeypatch
    ) -> None:
        _make_tree(tmp_path, 8)
        await upload(str(tmp_path), "runs/r2", store=store, skip_if_exists=True)
        log = _RequestLog(monkeypatch)

        out = await upload(str(tmp_path), "runs/r2", store=store, skip_if_exists=True)

        assert out.synced is False
        assert out.reason == "skipped:hash_match"
        assert log.puts == []
        assert log.heads == []
        # Nothing transferred, so there is nothing to read back: skip check only.
        assert log.listings == ["runs/r2/"]

    async def test_changed_file_alone_is_reuploaded_and_read_back(
        self, store, tmp_path, monkeypatch
    ) -> None:
        paths = _make_tree(tmp_path, 8)
        await upload(str(tmp_path), "runs/r3", store=store, skip_if_exists=True)
        paths[3].write_bytes(b"changed" * 8)
        log = _RequestLog(monkeypatch)

        out = await upload(str(tmp_path), "runs/r3", store=store, skip_if_exists=True)

        changed_key = f"runs/r3/{paths[3].relative_to(tmp_path).as_posix()}"
        assert out.synced is True
        assert log.puts == [changed_key]
        assert log.heads == []
        assert log.listings == ["runs/r3/", "runs/r3/"]
        sidecar = await _get_bytes(f"{changed_key}.sha256", store, normalize=False)
        assert sidecar is not None
        assert sidecar.decode().strip() == _hash_bytes(b"changed" * 8)

    async def test_readback_catches_a_dropped_object_and_withholds_sidecars(
        self, store, tmp_path, monkeypatch
    ) -> None:
        _make_tree(tmp_path, 4)
        from application_sdk.storage import transfer as transfer_mod

        real = transfer_mod._list_target_sizes
        calls = {"n": 0}

        async def dropping(prefix, st):
            calls["n"] += 1
            listed = await real(prefix, st)
            if calls["n"] >= 2:  # the post-transfer readback, not the skip check
                listed.pop("runs/r4/raw/t0/chunk-0.parquet")
            return listed

        monkeypatch.setattr(transfer_mod, "_list_target_sizes", dropping)

        with pytest.raises(StorageError, match="absent from the store's listing"):
            await upload(str(tmp_path), "runs/r4", store=store, skip_if_exists=True)

        # The objects landed, but no sidecar may advertise a batch the readback
        # rejected — same ordering guarantee as the per-object protocol.
        keys = set(await list_keys("runs/r4/", store, normalize=False))
        assert len(keys) == 4
        assert not any(k.endswith(".sha256") for k in keys)

    async def test_readback_catches_a_size_mismatch(
        self, store, tmp_path, monkeypatch
    ) -> None:
        _make_tree(tmp_path, 4)
        from application_sdk.storage import transfer as transfer_mod

        real = transfer_mod._list_target_sizes

        async def short_by_one(prefix, st):
            listed = await real(prefix, st)
            key = "runs/r5/raw/t1/chunk-1.parquet"
            if key in listed:
                listed[key] -= 1
            return listed

        monkeypatch.setattr(transfer_mod, "_list_target_sizes", short_by_one)

        with pytest.raises(StorageError, match="Incomplete upload"):
            await upload(str(tmp_path), "runs/r5", store=store)

    async def test_verification_off_writes_sidecars_inline_and_skips_readback(
        self, store, tmp_path, monkeypatch
    ) -> None:
        _make_tree(tmp_path, 4)
        from application_sdk import constants

        monkeypatch.setattr(constants, "STORAGE_VERIFY_TRANSFERS", False)
        log = _RequestLog(monkeypatch)

        out = await upload(str(tmp_path), "runs/r6", store=store, skip_if_exists=True)

        assert out.synced is True
        assert log.heads == []
        assert log.listings == ["runs/r6/"]  # skip check only; nothing to read back
        keys = set(await list_keys("runs/r6/", store, normalize=False))
        assert sum(k.endswith(".sha256") for k in keys) == 4


class TestUploadDirectoryFanOut:
    """Small objects fan out on their own tier; large ones keep the narrow one."""

    @staticmethod
    def _track(monkeypatch, threshold: int) -> dict[str, int]:
        import application_sdk.storage.ops as ops_mod

        active = {"small": 0, "large": 0, "all": 0}
        peak = {"small": 0, "large": 0, "all": 0}
        real_upload = ops_mod.upload_file

        async def tracking(key, local_path, *a, **kw):
            tier = "small" if Path(local_path).stat().st_size <= threshold else "large"
            for t in (tier, "all"):
                active[t] += 1
                peak[t] = max(peak[t], active[t])
            try:
                await asyncio.sleep(0.02)  # hold the slot so peaks are observable
                return await real_upload(key, local_path, *a, **kw)
            finally:
                for t in (tier, "all"):
                    active[t] -= 1

        monkeypatch.setattr(ops_mod, "upload_file", tracking)
        return peak

    async def test_small_objects_fan_out_wider_than_large_ones(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "MAX_CONCURRENT_STORAGE_TRANSFERS", 2)
        monkeypatch.setattr(constants, "MAX_CONCURRENT_SMALL_TRANSFERS", 6)
        monkeypatch.setattr(constants, "STORAGE_SMALL_OBJECT_BYTES", 100)
        _make_tree(tmp_path / "small", 18, size=10)
        _make_tree(tmp_path / "large", 6, size=1000)
        peak = self._track(monkeypatch, threshold=100)

        out = await upload(str(tmp_path), "runs/r7", store=store)

        assert out.ref.file_count == 24
        assert peak["large"] <= 2
        assert 2 < peak["small"] <= 6

    async def test_explicit_max_concurrency_caps_both_tiers(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "MAX_CONCURRENT_SMALL_TRANSFERS", 32)
        monkeypatch.setattr(constants, "STORAGE_SMALL_OBJECT_BYTES", 100)
        _make_tree(tmp_path / "small", 8, size=10)
        _make_tree(tmp_path / "large", 4, size=1000)
        peak = self._track(monkeypatch, threshold=100)

        out = await upload(str(tmp_path), "runs/r8", store=store, max_concurrency=1)

        assert out.ref.file_count == 12
        assert peak["all"] == 1

    async def test_zero_threshold_disables_the_small_tier(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "MAX_CONCURRENT_STORAGE_TRANSFERS", 2)
        monkeypatch.setattr(constants, "MAX_CONCURRENT_SMALL_TRANSFERS", 16)
        monkeypatch.setattr(constants, "STORAGE_SMALL_OBJECT_BYTES", 0)
        _make_tree(tmp_path, 10, size=10)
        peak = self._track(monkeypatch, threshold=0)

        await upload(str(tmp_path), "runs/r9", store=store)

        assert peak["all"] <= 2

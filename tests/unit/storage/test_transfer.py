"""Unit tests for storage.transfer upload/download with MemoryStore."""

from __future__ import annotations

import sys

import pytest

from application_sdk.contracts.storage import UploadOutput
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.transfer import download, upload

_IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def store():
    return create_memory_store()


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

        async def _failing_upload_one(st, local_file, store_key, *, skip_if_exists):
            if "fail.txt" in str(local_file):
                raise RuntimeError("simulated upload failure")
            return await _original(
                st, local_file, store_key, skip_if_exists=skip_if_exists
            )

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

        obstore rejects ``..`` keys on put, so we patch ``list_keys`` to plant
        a hostile listing and assert the containment guard fires before any
        write happens (issue #1694).
        """
        from unittest.mock import AsyncMock, patch

        from application_sdk.storage.errors import StorageError

        dest = tmp_path / "dest"
        canary = tmp_path / "canary.txt"
        # Trailing slash in storage_path puts download() straight into prefix
        # mode, so only the prefix listing is consulted.
        with (
            patch(
                "application_sdk.storage.batch.list_keys",
                new=AsyncMock(return_value=["p/safe/../../canary.txt"]),
            ),
            pytest.raises(StorageError, match="Path traversal"),
        ):
            await download("p/", str(dest), store=store)
        assert not canary.exists()


class TestUploadDirectoryListingRace:
    """Reproduces the rglob listing race observed in production.

    When ``Path.rglob("*")`` returns empty (or partial) for a directory
    that actually contains files — caused by CPython's pathlib silently
    swallowing ``OSError`` mid-walk (cpython#146646) and/or APFS
    directory-metadata visibility lag on macOS under concurrent load —
    the directory branch of ``upload()`` silently returns
    ``file_count=0`` with no error signal. Downstream consumers
    branching on ``file_count == 0`` then drop entire pipeline stages.

    These tests inject the rglob transient via monkeypatch and assert
    the upload still finds the files. They FAIL on the current code
    (which uses ``pathlib.rglob`` directly) and PASS after the
    migration to ``safe_list_directory`` (which walks via
    ``os.scandir`` internally and is unaffected by the rglob mock).
    """

    async def test_upload_finds_files_when_rglob_returns_empty(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from pathlib import Path

        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")

        # Inject the listing race: Path.rglob returns empty even though
        # the directory has 3 files.
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        out = await upload(str(tmp_path), "race_prefix", store=store)

        # On main: file_count==0 (production silent-failure mode).
        # After fix: safe_list_directory bypasses rglob via os.scandir.
        assert out.ref.file_count == 3
        assert out.synced is True

    async def test_upload_finds_all_files_when_rglob_returns_partial(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """The subtler form of the bug: pathlib swallows OSError on one
        subdir mid-walk and returns a partial result. The caller sees
        an undercount that looks like a successful upload."""
        from pathlib import Path

        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        (tmp_path / "c.txt").write_bytes(b"c")

        # Return only 1 of the 3 files — simulating partial-truncation.
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
        """When a caller opts into raise_on_empty=True, a transient
        rglob result must not look like a quiet-day empty run.
        Pre-fix this would either raise StorageEmptyUploadError (wrong
        signal — there ARE files) or silently zero-count."""
        from pathlib import Path

        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")

        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        out = await upload(
            str(tmp_path), "race_prefix", store=store, raise_on_empty=True
        )

        assert out.ref.file_count == 2

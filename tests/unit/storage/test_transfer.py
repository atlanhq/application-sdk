"""Unit tests for storage.transfer upload/download with MemoryStore."""

from __future__ import annotations

import pytest

from application_sdk.contracts.storage import UploadOutput
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.transfer import download, upload


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
        f = tmp_path / "data.txt"
        f.write_bytes(b"x")
        with pytest.raises(ValueError, match="path traversal"):
            await upload(
                str(f), store=store, _app_prefix="run/123", storage_subdir="../../etc"
            )


class TestUploadSensitivePathBlocking:
    """Tests for blocking uploads from sensitive system paths."""

    async def test_etc_blocked(self, store) -> None:
        with pytest.raises(ValueError, match="sensitive system path"):
            await upload("/etc/passwd", store=store)

    async def test_proc_blocked(self, store) -> None:
        with pytest.raises(ValueError, match="sensitive system path"):
            await upload("/proc/self/environ", store=store)

    async def test_aws_dir_blocked(self, store, tmp_path) -> None:
        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir()
        creds = aws_dir / "credentials"
        creds.write_bytes(b"secret")
        with pytest.raises(ValueError, match="sensitive directory"):
            await upload(str(creds), store=store)

    async def test_ssh_dir_blocked(self, store, tmp_path) -> None:
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_rsa"
        key.write_bytes(b"private-key")
        with pytest.raises(ValueError, match="sensitive directory"):
            await upload(str(key), store=store)

    async def test_env_file_blocked(self, store, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_bytes(b"SECRET=value")
        with pytest.raises(ValueError, match="sensitive file"):
            await upload(str(env_file), store=store)

    async def test_env_local_file_blocked(self, store, tmp_path) -> None:
        env_file = tmp_path / ".env.local"
        env_file.write_bytes(b"SECRET=value")
        with pytest.raises(ValueError, match="sensitive file"):
            await upload(str(env_file), store=store)

    async def test_path_traversal_blocked(self, store, tmp_path) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
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
        monkeypatch.setenv("ATLAN_UPLOAD_FILE_BLOCKED_PATHS", "custom_secrets,.credentials")
        with pytest.raises(ValueError, match="ATLAN_UPLOAD_FILE_BLOCKED_PATHS"):
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

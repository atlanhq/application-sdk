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


class TestUploadDirectoryListingTransient:
    """PART-1148: absorb the rglob/iterdir listing transient inside the SDK.

    Before the fix, the directory branch of :func:`upload` enumerated the
    local tree with a single ``src.rglob("*")`` walk and trusted the
    result. When that walk returned empty for a directory that DID
    contain files (filesystem-listing transient, observed on macOS APFS
    under concurrent activity load), the SDK silently completed with
    ``ref.file_count=0, reason="skipped:hash_match"``. Downstream
    consumers branching on ``ref.file_count == 0`` then dropped the
    whole stage — invisible to telemetry.

    The fix has two parts:

    1. Internal retry-with-backoff (50/100/200 ms) when ``rglob`` and
       ``iterdir`` disagree on whether the directory has top-level
       files. Caller code never sees the transient; the library
       absorbs it. Standard pattern across cloud SDKs for transient
       I/O.
    2. Observability: emit ``reason="empty"`` (not the misleading
       ``"skipped:hash_match"``) for genuinely empty input.

    Tests below pin every branch of the new behaviour:
    happy-path / quiet-day-empty / one-retry recovery / two-retry
    recovery / persistent disagreement raises / race-takes-precedence
    over raise_on_empty / no false positive on subdir-only / explicit
    top-level-only scope.
    """

    @pytest.fixture
    def captured_sleeps(self, monkeypatch):
        """Mock ``asyncio.sleep`` inside transfer.py — capture durations, skip waits."""
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(
            "application_sdk.storage.transfer.asyncio.sleep", _fake_sleep
        )
        return sleeps

    async def test_genuinely_empty_dir_default_returns_empty_reason(
        self, store, tmp_path, captured_sleeps
    ) -> None:
        """Empty input + default ``raise_on_empty=False`` → silent quiet-day path.

        Pins both BLDX-1255 contract (silent ``file_count=0``) AND the
        observability correction (no more misleading ``skipped:hash_match``
        reason on empty input).
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        out = await upload(str(empty), "myprefix", store=store)
        assert out.ref.file_count == 0
        assert out.synced is False
        assert out.reason == "empty", (
            f"reason={out.reason!r}: empty input must report a distinct "
            f"reason. 'skipped:hash_match' falsely implies hash-match "
            f"skipping happened."
        )
        assert captured_sleeps == []

    async def test_genuinely_empty_dir_with_raise_on_empty_still_raises(
        self, store, tmp_path, captured_sleeps
    ) -> None:
        """Existing BLDX-1255 opt-in path preserved unchanged."""
        from application_sdk.storage.errors import StorageEmptyUploadError

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(StorageEmptyUploadError, match="contains zero files"):
            await upload(str(empty), "myprefix", store=store, raise_on_empty=True)
        assert captured_sleeps == []

    async def test_happy_path_no_sleeps(self, store, tmp_path, captured_sleeps) -> None:
        """When rglob succeeds first try, retries never fire — zero latency cost."""
        (tmp_path / "data.txt").write_bytes(b"hello")
        out = await upload(str(tmp_path), "myprefix", store=store)
        assert out.ref.file_count == 1
        assert out.reason == "uploaded"
        assert captured_sleeps == []

    async def test_transient_recovers_on_first_retry(
        self, store, tmp_path, captured_sleeps, monkeypatch
    ) -> None:
        """One empty rglob → 50ms backoff → next call returns the file → uploads normally.

        Models the macOS APFS readdir-after-write window. Caller observes
        a normal ``reason="uploaded"`` result — no surfacing of the
        transient.
        """
        from pathlib import Path

        local_dir = tmp_path / "raced"
        local_dir.mkdir()
        (local_dir / "file.txt").write_bytes(b"data")

        original_rglob = Path.rglob
        n = {"calls": 0}

        def _flaky_rglob(self, pattern):
            n["calls"] += 1
            return iter([]) if n["calls"] == 1 else original_rglob(self, pattern)

        monkeypatch.setattr(Path, "rglob", _flaky_rglob)

        out = await upload(str(local_dir), "raced", store=store)
        assert out.ref.file_count == 1
        assert out.reason == "uploaded"
        assert captured_sleeps == [0.05]

    async def test_transient_recovers_on_second_retry(
        self, store, tmp_path, captured_sleeps, monkeypatch
    ) -> None:
        """Two empty rglobs (50, 100ms backoffs) → third call recovers → succeeds."""
        from pathlib import Path

        local_dir = tmp_path / "raced"
        local_dir.mkdir()
        (local_dir / "file.txt").write_bytes(b"data")

        original_rglob = Path.rglob
        n = {"calls": 0}

        def _flaky_rglob(self, pattern):
            n["calls"] += 1
            return iter([]) if n["calls"] <= 2 else original_rglob(self, pattern)

        monkeypatch.setattr(Path, "rglob", _flaky_rglob)

        out = await upload(str(local_dir), "raced", store=store)
        assert out.ref.file_count == 1
        assert out.reason == "uploaded"
        assert captured_sleeps == [0.05, 0.1]

    async def test_persistent_disagreement_raises_after_retries(
        self, store, tmp_path, captured_sleeps, monkeypatch
    ) -> None:
        """rglob STAYS empty across all three backoffs → ``StorageError``.

        Three retries (50/100/200 ms) fire before the raise. Caller
        knows it's not a transient — either truly broken filesystem or
        an external observer that keeps removing files.
        """
        from pathlib import Path

        from application_sdk.storage.errors import StorageError

        local_dir = tmp_path / "broken"
        local_dir.mkdir()
        (local_dir / "file.txt").write_bytes(b"data")

        monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter([]))

        with pytest.raises(StorageError, match="persistently inconsistent"):
            await upload(str(local_dir), "broken", store=store)
        assert captured_sleeps == [0.05, 0.1, 0.2]

    async def test_persistent_disagreement_precedes_raise_on_empty(
        self, store, tmp_path, captured_sleeps, monkeypatch
    ) -> None:
        """Race raise takes precedence over BLDX-1255's empty-upload raise.

        Without this ordering, a caller opting in to ``raise_on_empty=True``
        would see ``StorageEmptyUploadError("extract produced no output")``
        when the actual cause is the SDK's inability to list the
        already-present file — sending them looking in the wrong place.
        """
        from pathlib import Path

        from application_sdk.storage.errors import StorageError

        local_dir = tmp_path / "raced"
        local_dir.mkdir()
        (local_dir / "file.txt").write_bytes(b"x")

        monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter([]))

        with pytest.raises(StorageError, match="persistently inconsistent"):
            await upload(str(local_dir), "raced", store=store, raise_on_empty=True)

    async def test_no_false_positive_on_only_empty_subdir(
        self, store, tmp_path, captured_sleeps
    ) -> None:
        """A directory whose only child is an empty subdir is genuinely empty.

        The iterdir disagreement check scopes to top-level FILES
        (matching ``rglob``'s ``is_file()`` filter). A subdir at top
        level isn't a file → no false retry → correct genuine-empty
        report.
        """
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "empty_sub").mkdir()
        out = await upload(str(outer), "outer", store=store)
        assert out.ref.file_count == 0
        assert out.reason == "empty"
        assert captured_sleeps == []

    async def test_iterdir_check_is_top_level_only(
        self, store, tmp_path, captured_sleeps, monkeypatch
    ) -> None:
        """The cheap iterdir check is scoped to top level (documented limit).

        When rglob returns empty AND the only top-level entry is a
        subdir (even if that subdir contains a deeply-nested file), the
        check correctly returns "no race" — matching the rglob
        ``is_file()`` filter. Deep-tree transients are out of scope; if
        they ever surface in the wild we'd add a recursive secondary
        check. This test pins the current scope as intentional.
        """
        from pathlib import Path

        outer = tmp_path / "outer"
        outer.mkdir()
        inner = outer / "subdir"
        inner.mkdir()
        (inner / "deep.txt").write_bytes(b"nested")

        monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter([]))

        out = await upload(str(outer), "outer", store=store)
        assert out.ref.file_count == 0
        assert out.reason == "empty"
        assert captured_sleeps == []


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

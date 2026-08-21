"""Unit tests for storage.batch using MemoryStore (no real I/O)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import obstore
import pytest

from application_sdk.storage import batch as batch_module
from application_sdk.storage.batch import (
    delete_prefix,
    download_prefix,
    list_keys,
    upload_file_from_bytes,
    upload_prefix,
)
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_local_store, create_memory_store
from application_sdk.storage.ops import _get_bytes, _put


@pytest.fixture
def store():
    return create_memory_store()


# ---------------------------------------------------------------------------
# list_keys
# ---------------------------------------------------------------------------


class TestListKeys:
    async def test_normalises_and_appends_trailing_slash(self, store) -> None:
        await _put("foo/a.txt", b"a", store, normalize=False)
        await _put("foo_other/b.txt", b"b", store, normalize=False)
        # without trailing slash; normalize=True must add one to avoid
        # matching siblings like "foo_other/".
        keys = await list_keys("foo", store)
        assert "foo/a.txt" in keys
        assert "foo_other/b.txt" not in keys

    async def test_normalize_false_uses_prefix_exactly(self, store) -> None:
        await _put("raw/x.txt", b"x", store, normalize=False)
        keys = await list_keys("raw/", store, normalize=False)
        assert keys == ["raw/x.txt"]

    async def test_suffix_filter(self, store) -> None:
        await _put("a/file.parquet", b"1", store, normalize=False)
        await _put("a/file.json", b"2", store, normalize=False)
        keys = await list_keys("a/", store, suffix=".parquet", normalize=False)
        assert keys == ["a/file.parquet"]

    async def test_empty_prefix_lists_everything(self, store) -> None:
        await _put("a.txt", b"1", store, normalize=False)
        await _put("b/c.txt", b"2", store, normalize=False)
        keys = await list_keys("", store)
        assert sorted(keys) == ["a.txt", "b/c.txt"]

    async def test_underlying_failure_wraps_as_storage_error(self, store) -> None:
        """If obstore.list raises, list_keys raises StorageError.

        BLDX-1129 anchor: this exercises the function-local
        `from application_sdk.storage.errors import StorageError` import.
        """

        def boom(*args, **kwargs):
            raise RuntimeError("listing exploded")

        with patch("application_sdk.storage.batch.obstore.list", side_effect=boom):
            with pytest.raises(StorageError) as exc_info:
                await list_keys("anything/", store, normalize=False)
        assert "Failed to list keys" in str(exc_info.value)


# ---------------------------------------------------------------------------
# delete_prefix
# ---------------------------------------------------------------------------


class _ForeignBaseError(BaseException):
    """A leaf ``ops.delete``'s ``except Exception`` cannot wrap.

    Not ``SystemExit`` / ``KeyboardInterrupt``: asyncio re-raises those directly
    into the event loop, so they never reach the TaskGroup's ExceptionGroup and
    would not exercise the branch under test.
    """


def _bulk_delete_raising_not_found(vanished_key: str):
    """Side effect where the bulk (list) delete reports *vanished_key* as gone.

    Single-key deletes still reach the real store, so ``delete_prefix``'s
    per-key fallback behaves as it would against GCS or Azure.  Patching
    ``batch.obstore.delete_async`` patches the shared obstore module, which
    ``ops.delete`` calls too — hence the passthrough rather than a blanket raise.
    """
    real_delete_async = obstore.delete_async

    async def side_effect(target, paths):
        if isinstance(paths, str):
            return await real_delete_async(target, paths)
        raise FileNotFoundError(
            f"Object at location {vanished_key} not found: "
            "Error performing DeleteObjects request"
        )

    return side_effect


class TestDeletePrefix:
    async def test_deletes_all_under_prefix(self, store) -> None:
        await _put("p/a.txt", b"1", store, normalize=False)
        await _put("p/b.txt", b"2", store, normalize=False)
        await _put("other/c.txt", b"3", store, normalize=False)

        n = await delete_prefix("p/", store, normalize=False)
        assert n == 2
        # other/ untouched
        assert await _get_bytes("other/c.txt", store, normalize=False) == b"3"
        assert await _get_bytes("p/a.txt", store, normalize=False) is None

    async def test_empty_prefix_deletes_nothing(self, store) -> None:
        # No matches under "missing/"
        n = await delete_prefix("missing/", store, normalize=False)
        assert n == 0

    async def test_delete_async_failure_raises_storage_error(self, store) -> None:
        """A genuine bulk-delete failure (not a vanished key) stays fatal:
        delete_prefix wraps it as StorageError rather than swallowing or retrying."""
        await _put("q/a.txt", b"1", store, normalize=False)

        async def boom(*args, **kwargs):
            raise RuntimeError("connection reset by peer")

        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async", side_effect=boom
            ),
            pytest.raises(StorageError) as exc_info,
        ):
            await delete_prefix("q/", store, normalize=False)
        assert "Failed to delete" in str(exc_info.value)

    async def test_vanished_key_during_bulk_delete_is_benign(self, store) -> None:
        """FND-341: a key that disappears between the listing and the bulk delete
        must not fail the caller — the end state it wants is already true.

        The per-key fallback also has to finish the job: obstore stops the bulk
        delete at the first per-key failure, so the keys behind it are still
        there and the prefix would otherwise be left half-deleted.
        """
        await _put("s/a.txt", b"1", store, normalize=False)
        await _put("s/b.txt", b"2", store, normalize=False)

        with patch(
            "application_sdk.storage.batch.obstore.delete_async",
            side_effect=_bulk_delete_raising_not_found("s/a.txt"),
        ):
            n = await delete_prefix("s/", store, normalize=False)

        assert n == 2  # both keys still existed; the per-key pass removed them
        assert await _get_bytes("s/a.txt", store, normalize=False) is None
        assert await _get_bytes("s/b.txt", store, normalize=False) is None

    async def test_vanished_key_is_not_counted_as_deleted(self, tmp_path) -> None:
        """FND-341: the returned count reflects objects this call removed, so a
        key the race already took is excluded.

        Uses a LocalStore rather than the MemoryStore fixture: only stores that
        report a missing key (local, GCS, Azure) can show the exclusion — S3 and
        MemoryStore treat deleting a gone key as success, so ``ops.delete``
        legitimately counts it.  The head probe is pinned to the Windows
        LocalStore directory-stat response (see below) so the test fails on
        every OS if the root-marker probe stops recognising a directory
        collision as "no marker".
        """
        store = create_local_store(tmp_path / "store")
        await _put("t/gone.txt", b"1", store, normalize=False)
        await _put("t/here.txt", b"2", store, normalize=False)

        real_delete_async = obstore.delete_async

        async def bulk_deletes_one_then_hits_a_vanished_key(target, paths):
            if isinstance(paths, str):
                return await real_delete_async(target, paths)
            # Emulate a partially-applied bulk delete: "t/gone.txt" is removed by
            # the concurrent writer we are racing, then the store reports it.
            await real_delete_async(target, "t/gone.txt")
            raise FileNotFoundError(
                "Object at location t/gone.txt not found: "
                "Error performing DeleteObjects request"
            )

        # The root-marker probe for "t/" is the bare key "t", which the local
        # store maps onto the *directory* holding the two keys.  POSIX reports
        # that stat as not-found, but the Windows LocalStore raises a
        # GenericError "Unable to open file …: Access is denied" — a directory
        # can never be an object, so the probe must read it as "no marker" on
        # every OS.  Pin that exact response so this regression is exercised
        # everywhere.
        real_head_async = obstore.head_async

        async def head_reports_directory_as_denied(target, path):
            if path == "t":
                raise obstore.exceptions.GenericError(
                    "Generic LocalFileSystem error: Unable to open file "
                    f"{tmp_path / 'store' / 't'}: Access is denied. (os error 5)"
                )
            return await real_head_async(target, path)

        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async",
                side_effect=bulk_deletes_one_then_hits_a_vanished_key,
            ),
            patch(
                "application_sdk.storage.batch.obstore.head_async",
                side_effect=head_reports_directory_as_denied,
            ),
        ):
            n = await delete_prefix("t/", store, normalize=False)

        assert n == 1  # only "t/here.txt" was actually removed by this call
        assert await _get_bytes("t/here.txt", store, normalize=False) is None

    async def test_vanished_key_is_reported_as_a_warning(self, store) -> None:
        """FND-341: the 'two apps share this prefix' signal is kept — as a
        WARNING naming the prefix, not as an exception."""
        await _put("u/a.txt", b"1", store, normalize=False)

        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async",
                side_effect=_bulk_delete_raising_not_found("u/a.txt"),
            ),
            patch.object(batch_module.logger, "warning") as warn,
        ):
            await delete_prefix("u/", store, normalize=False)

        assert warn.call_count == 1
        message, *args = warn.call_args.args
        assert "vanished" in message
        assert "u/" in args
        assert "u/a.txt" in str(args[-1])

    async def test_genuine_failure_in_per_key_fallback_still_raises(
        self, store
    ) -> None:
        """The fallback is idempotent, not forgiving: a real error during the
        per-key pass surfaces as StorageError."""
        await _put("v/a.txt", b"1", store, normalize=False)

        async def not_found_then_denied(target, paths):
            if isinstance(paths, str):
                raise RuntimeError("permission denied")
            raise FileNotFoundError("Object at location v/a.txt not found")

        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async",
                side_effect=not_found_then_denied,
            ),
            pytest.raises(StorageError) as exc_info,
        ):
            await delete_prefix("v/", store, normalize=False)
        assert "Failed to delete key" in str(exc_info.value)

    async def test_head_probe_non404_raises_storage_error(self, store) -> None:
        """If head_async raises a non-404 error during root-marker probe,
        delete_prefix surfaces it as StorageError rather than silently skipping."""
        await _put("r/a.txt", b"1", store, normalize=False)

        async def boom(*args, **kwargs):
            raise RuntimeError("permission denied")

        with (
            patch("application_sdk.storage.batch.obstore.head_async", side_effect=boom),
            pytest.raises(StorageError) as exc_info,
        ):
            await delete_prefix("r", store)
        assert "Failed to check root marker" in str(exc_info.value)

    async def test_foreign_failure_in_fallback_is_not_demoted_to_a_cause(
        self, store
    ) -> None:
        """Unwrapping the ExceptionGroup is only safe when every leaf is a
        StorageError. A leaf that is not (here a BaseException, which
        ``ops.delete``'s ``except Exception`` does not wrap) must reach the
        caller as the group — demoting it to ``__cause__`` would leave it
        reachable in a traceback but invisible to an ``except`` clause.
        """
        await _put("x/a.txt", b"1", store, normalize=False)

        async def not_found_then_foreign(target, paths):
            if isinstance(paths, str):
                raise _ForeignBaseError("not an Exception subclass")
            raise FileNotFoundError("Object at location x/a.txt not found")

        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async",
                side_effect=not_found_then_foreign,
            ),
            pytest.raises(BaseExceptionGroup) as exc_info,
        ):
            await delete_prefix("x/", store, normalize=False)

        assert [type(leaf) for leaf in exc_info.value.exceptions] == [_ForeignBaseError]

    async def test_fallback_awaits_sibling_cancellation_before_raising(
        self, store
    ) -> None:
        """FND-341: the per-key fallback must not leave a delete in flight.

        A delete that completes server-side *after* the caller has seen the
        failure can land on a prefix the caller's retry has already rewritten, so
        cancellation has to be awaited, not merely requested — the difference
        between a TaskGroup and ``gather``.  Pinned by observing that the
        surviving sibling has finished unwinding before the error propagates:
        under ``gather`` the ``finally`` below would not have run yet.
        """
        await _put("w/bad.txt", b"1", store, normalize=False)
        await _put("w/slow.txt", b"2", store, normalize=False)

        unwound: list[str] = []
        never_set = asyncio.Event()  # the sibling can only end via cancellation

        async def one_fails_one_hangs(target, paths):
            if not isinstance(paths, str):
                raise FileNotFoundError("Object at location w/bad.txt not found")
            if paths == "w/bad.txt":
                raise RuntimeError("permission denied")
            try:
                await never_set.wait()
            finally:
                unwound.append(paths)

        # wait_for bounds the whole call: if cancellation ever stops being
        # delivered, this fails in seconds with a TimeoutError instead of
        # parking the suite on the sibling.
        with (
            patch(
                "application_sdk.storage.batch.obstore.delete_async",
                side_effect=one_fails_one_hangs,
            ),
            pytest.raises(StorageError),
        ):
            await asyncio.wait_for(
                delete_prefix("w/", store, normalize=False), timeout=5
            )

        assert unwound == ["w/slow.txt"]

    @pytest.mark.parametrize(
        "exc",
        [
            obstore.exceptions.GenericError("Access is denied. (os error 5)"),
            PermissionError("Access is denied"),
        ],
        ids=["obstore-generic", "builtin-permissionerror"],
    )
    async def test_head_probe_plain_access_denied_still_raises(
        self, store, exc
    ) -> None:
        """The directory-collision relaxation is conjunctive: a *plain*
        "Access is denied" (no "Unable to open file …" directory-stat shape)
        is a permission failure, not a missing marker, and must stay fatal.

        Covers both shapes it can arrive in: an obstore ``GenericError`` carrying
        the OS message, and a bare built-in ``PermissionError`` — the latter is an
        ``OSError``, so nothing about its class makes it look missing either.
        """
        await _put("r2/a.txt", b"1", store, normalize=False)

        async def denied(*args, **kwargs):
            raise exc

        with (
            patch(
                "application_sdk.storage.batch.obstore.head_async", side_effect=denied
            ),
            pytest.raises(StorageError) as exc_info,
        ):
            await delete_prefix("r2", store)
        assert "Failed to check root marker" in str(exc_info.value)


# ---------------------------------------------------------------------------
# download_prefix
# ---------------------------------------------------------------------------


class TestDownloadPrefix:
    async def test_downloads_all_keys_to_local_dir(self, store, tmp_path) -> None:
        await _put("dl/sub/a.txt", b"alpha", store, normalize=False)
        await _put("dl/b.txt", b"beta", store, normalize=False)

        dests = await download_prefix(
            "dl/", tmp_path, store, normalize=False, max_concurrency=2
        )
        # Each downloaded file's local path must exist with correct content
        assert len(dests) == 2
        for d in dests:
            assert Path(d).exists()
        assert (tmp_path / "dl" / "sub" / "a.txt").read_bytes() == b"alpha"
        assert (tmp_path / "dl" / "b.txt").read_bytes() == b"beta"

    async def test_strip_prefix_reproduces_only_the_tree_under_prefix(
        self, store, tmp_path
    ) -> None:
        """strip_prefix=True drops the prefix instead of repeating it locally."""
        await _put("run/transformed/table/a.json", b"alpha", store, normalize=False)
        await _put("run/transformed/column/b.json", b"beta", store, normalize=False)

        dests = await download_prefix(
            "run/transformed",
            tmp_path,
            store,
            normalize=False,
            strip_prefix=True,
        )

        assert len(dests) == 2
        assert (tmp_path / "table" / "a.json").read_bytes() == b"alpha"
        assert (tmp_path / "column" / "b.json").read_bytes() == b"beta"
        # The defining property: no second copy of the prefix under local_dir.
        assert not (tmp_path / "run").exists()

    async def test_strip_prefix_handles_trailing_slash(self, store, tmp_path) -> None:
        """A slash-terminated prefix strips identically (no leading-slash join)."""
        await _put("run/transformed/table/a.json", b"alpha", store, normalize=False)

        await download_prefix(
            "run/transformed/", tmp_path, store, normalize=False, strip_prefix=True
        )

        assert (tmp_path / "table" / "a.json").read_bytes() == b"alpha"

    async def test_strip_prefix_strips_the_normalised_prefix(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """A v2-style staging path strips as its normalised ``artifacts/...`` key.

        Stripping the caller's raw argument would miss — the listing matched the
        normalised form — and every file would land under a full copy of the key
        path instead.
        """
        from application_sdk import constants

        monkeypatch.setattr(constants, "TEMPORARY_PATH", str(tmp_path / "staging"))
        await _put(
            "artifacts/run/transformed/table/a.json", b"a", store, normalize=False
        )

        dest = tmp_path / "out"
        await download_prefix(
            f"{tmp_path}/staging/artifacts/run/transformed",
            dest,
            store,
            strip_prefix=True,
        )

        assert (dest / "table" / "a.json").read_bytes() == b"a"
        assert not (dest / "artifacts").exists()

    async def test_default_keeps_full_store_path(self, store, tmp_path) -> None:
        """Without strip_prefix the full key layout is preserved (unchanged default)."""
        await _put("run/transformed/table/a.json", b"alpha", store, normalize=False)

        await download_prefix("run/transformed", tmp_path, store, normalize=False)

        assert (
            tmp_path / "run" / "transformed" / "table" / "a.json"
        ).read_bytes() == b"alpha"

    async def test_strip_prefix_does_not_misstrip_sibling_keys(self) -> None:
        """The strip matches on a path boundary, never a bare string prefix.

        A sibling key sharing a string prefix (``a/b2/x`` under strip ``a/b``)
        must be preserved whole, not mis-stripped to ``2/x``. The store listing
        is itself boundary-aware (obstore returns only keys under ``a/b/``), so
        this exercises ``_local_relative_key`` directly to pin the defensive
        contract independent of any backend's listing semantics.
        """
        from application_sdk.storage.batch import _local_relative_key

        assert _local_relative_key("a/b/file.json", "a/b") == "file.json"
        assert _local_relative_key("a/b2/file.json", "a/b") == "a/b2/file.json"
        assert _local_relative_key("a/b", "a/b") == "b"

    async def test_suffix_filter_restricts_download(self, store, tmp_path) -> None:
        await _put("d/x.parquet", b"p", store, normalize=False)
        await _put("d/x.json", b"j", store, normalize=False)
        dests = await download_prefix(
            "d/", tmp_path, store, suffix=".parquet", normalize=False
        )
        assert len(dests) == 1
        assert dests[0].endswith(".parquet")

    async def test_empty_prefix_returns_no_files(self, store, tmp_path) -> None:
        dests = await download_prefix("nothing/", tmp_path, store, normalize=False)
        assert dests == []

    async def test_path_traversal_in_key_rejected(self, store, tmp_path) -> None:
        """A listed key containing ``..`` must not write outside *local_dir*.

        obstore rejects ``..`` keys on put, so we plant a hostile listing via
        a patched ``list_keys_with_meta`` and assert the containment guard fires
        before any local write happens (issue #1694).
        """
        dest = tmp_path / "dest"
        canary = tmp_path / "canary.txt"
        with (
            patch(
                "application_sdk.storage.batch.list_keys_with_meta",
                new=AsyncMock(return_value=[("safe/../../canary.txt", 10, None)]),
            ),
            pytest.raises(StorageError, match="Path traversal"),
        ):
            await download_prefix("p/", dest, store, normalize=False)
        assert not canary.exists()


# ---------------------------------------------------------------------------
# upload_prefix
# ---------------------------------------------------------------------------


class TestUploadPrefix:
    async def test_uploads_directory_tree(self, store, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"alpha")
        (tmp_path / "b.txt").write_bytes(b"beta")

        keys = await upload_prefix(tmp_path, "remote", store, normalize=False)
        assert sorted(keys) == ["remote/b.txt", "remote/sub/a.txt"]
        # Verify content uploaded
        assert await _get_bytes("remote/b.txt", store, normalize=False) == b"beta"
        assert await _get_bytes("remote/sub/a.txt", store, normalize=False) == b"alpha"

    async def test_skips_symlinks(self, store, tmp_path) -> None:
        # Real file, plus a symlink that should be skipped (path-traversal guard).
        (tmp_path / "real.txt").write_bytes(b"data")
        target = tmp_path / "outside.txt"
        target.write_bytes(b"shouldnt-be-uploaded")
        try:
            (tmp_path / "link.txt").symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")
        # Remove target so a follow would fail loudly (extra check)
        keys = await upload_prefix(tmp_path, "out", store, normalize=False)
        # link.txt must not be in the uploaded keys
        assert "out/link.txt" not in keys
        assert "out/real.txt" in keys

    async def test_empty_prefix_uses_relative_paths(self, store, tmp_path) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        keys = await upload_prefix(tmp_path, "", store, normalize=False)
        assert keys == ["f.txt"]

    async def test_normalize_true_normalises_prefix(self, store, tmp_path) -> None:
        """Default normalize=True: leading slash is stripped from prefix."""
        (tmp_path / "f.txt").write_bytes(b"x")
        keys = await upload_prefix(tmp_path, "/abs/prefix", store, normalize=True)
        assert keys == ["abs/prefix/f.txt"]


# ---------------------------------------------------------------------------
# upload_file_from_bytes
# ---------------------------------------------------------------------------


class TestUploadFileFromBytes:
    async def test_uploads_bytes_via_temp_file(self, store) -> None:
        """BLDX-1129 anchor: exercises function-local `import tempfile`."""
        sha = await upload_file_from_bytes(
            "k/blob.bin", b"hello bytes", store, normalize=False
        )
        # SHA-256 hex string
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)
        # Roundtrip readable
        assert await _get_bytes("k/blob.bin", store, normalize=False) == b"hello bytes"

    async def test_cleanup_failure_swallowed(self, store) -> None:
        """If os.unlink raises OSError, the upload still succeeds."""
        original_unlink = os.unlink

        def _flaky_unlink(path):
            # Raise once, then call through so test cleanup still happens
            _flaky_unlink.calls += 1
            if _flaky_unlink.calls == 1:
                raise OSError("temp file already gone")
            return original_unlink(path)

        _flaky_unlink.calls = 0
        with patch("application_sdk.storage.batch.os.unlink", _flaky_unlink):
            sha = await upload_file_from_bytes(
                "k/quiet.bin", b"data", store, normalize=False
            )
        assert len(sha) == 64
        assert _flaky_unlink.calls == 1

    async def test_empty_bytes(self, store) -> None:
        sha = await upload_file_from_bytes("k/zero.bin", b"", store, normalize=False)
        assert len(sha) == 64
        assert await _get_bytes("k/zero.bin", store, normalize=False) == b""


# ---------------------------------------------------------------------------
# Misc edge cases
# ---------------------------------------------------------------------------


class TestStoreResolution:
    async def test_list_keys_no_store_raises_runtime_error(self) -> None:
        """When no store is supplied AND no infra context is set, raise."""
        # _resolve_store raises RuntimeError under these conditions; surfaces as StorageError-wrapped or RuntimeError
        with (
            patch(
                "application_sdk.storage.batch._resolve_store",
                side_effect=RuntimeError("no store"),
            ),
            pytest.raises(RuntimeError),
        ):
            await list_keys("p/", None)

    async def test_download_prefix_passes_store_through(self, store, tmp_path) -> None:
        """download_prefix delegates list_keys with the user-supplied store.

        Ensures the resolved-store contract is honored end-to-end.
        """
        await _put("only/x.txt", b"y", store, normalize=False)
        dests = await download_prefix("only/", tmp_path, store=store, normalize=False)
        assert len(dests) == 1


class TestUploadPrefixConcurrencyArg:
    async def test_max_concurrency_does_not_break_small_uploads(
        self, store, tmp_path
    ) -> None:
        # Single file, max_concurrency=1 → still works.
        (tmp_path / "f.txt").write_bytes(b"alpha")
        keys = await upload_prefix(
            tmp_path, "p", store, normalize=False, max_concurrency=1
        )
        assert keys == ["p/f.txt"]

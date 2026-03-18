"""Unit tests for storage ops using MemoryStore."""

from __future__ import annotations

import os

import pytest

from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import (
    delete,
    get_bytes,
    get_content,
    list_keys,
    normalize_key,
    put,
)


@pytest.fixture
def store():
    return create_memory_store()


class TestPutAndGet:
    async def test_put_then_get_returns_data(self, store) -> None:
        await put("hello.txt", b"world", store)
        result = await get_bytes("hello.txt", store)
        assert result == b"world"

    async def test_get_missing_key_returns_none(self, store) -> None:
        result = await get_bytes("nonexistent/key.bin", store)
        assert result is None

    async def test_put_overwrites(self, store) -> None:
        await put("key", b"v1", store)
        await put("key", b"v2", store)
        assert await get_bytes("key", store) == b"v2"

    async def test_put_empty_bytes(self, store) -> None:
        await put("empty", b"", store)
        result = await get_bytes("empty", store)
        assert result == b""

    async def test_get_content_alias(self, store) -> None:
        """get_content is the v2-compatible alias for get_bytes."""
        await put("alias.txt", b"v2compat", store)
        result = await get_content("alias.txt", store)
        assert result == b"v2compat"


class TestDelete:
    async def test_delete_existing_key(self, store) -> None:
        await put("del-me", b"data", store)
        deleted = await delete("del-me", store)
        assert deleted is True
        assert await get_bytes("del-me", store) is None

    async def test_delete_missing_key_does_not_raise(self, store) -> None:
        # MemoryStore silently succeeds on delete of non-existent key
        result = await delete("not-there", store)
        assert isinstance(result, bool)


class TestListKeys:
    async def test_list_all_keys(self, store) -> None:
        await put("a/b.txt", b"1", store)
        await put("a/c.txt", b"2", store)
        keys = await list_keys(store=store)
        assert "a/b.txt" in keys
        assert "a/c.txt" in keys

    async def test_list_with_prefix(self, store) -> None:
        await put("docs/x.txt", b"x", store)
        await put("docs/y.txt", b"y", store)
        await put("images/z.png", b"z", store)
        keys = await list_keys("docs/", store)
        assert "docs/x.txt" in keys
        assert "docs/y.txt" in keys
        assert "images/z.png" not in keys

    async def test_list_empty_store(self, store) -> None:
        keys = await list_keys(store=store)
        assert keys == []


class TestNormalizeKey:
    """Tests for normalize_key() — v2-compatible path normalisation."""

    def test_already_clean_key_is_unchanged(self) -> None:
        assert (
            normalize_key("artifacts/apps/foo/bar.jsonl")
            == "artifacts/apps/foo/bar.jsonl"
        )

    def test_leading_slash_is_stripped(self) -> None:
        assert normalize_key("/artifacts/foo.txt") == "artifacts/foo.txt"

    def test_trailing_slash_is_stripped(self) -> None:
        assert normalize_key("artifacts/foo/") == "artifacts/foo"

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_key("") == ""

    def test_staging_path_strips_temporary_prefix(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        staging = os.path.join(TEMPORARY_PATH, "artifacts/apps/myapp/run-1/out.json")
        assert normalize_key(staging) == "artifacts/apps/myapp/run-1/out.json"

    def test_staging_root_returns_empty(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        assert normalize_key(TEMPORARY_PATH) == ""

    def test_absolute_path_strips_leading_slash(self) -> None:
        assert normalize_key("/data/output.parquet") == "data/output.parquet"

    def test_file_refs_key_is_unchanged(self) -> None:
        assert normalize_key("file_refs/abc123.jsonl") == "file_refs/abc123.jsonl"


class TestNormalizeIntegration:
    """normalize=True (default) round-trips staging paths transparently."""

    async def test_put_staging_path_readable_as_store_key(self, store) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        staging = os.path.join(TEMPORARY_PATH, "artifacts/apps/app/wf/run/data.bin")
        await put(staging, b"payload", store)
        # Can be retrieved with the normalised key
        result = await get_bytes("artifacts/apps/app/wf/run/data.bin", store)
        assert result == b"payload"

    async def test_put_and_get_with_normalize_false_uses_exact_key(self, store) -> None:
        await put("exact/key.bin", b"data", store, normalize=False)
        result = await get_bytes("exact/key.bin", store, normalize=False)
        assert result == b"data"

    async def test_list_keys_adds_trailing_slash_to_prefix(self, store) -> None:
        await put("docs/a.txt", b"a", store, normalize=False)
        await put("docs_extra/b.txt", b"b", store, normalize=False)
        # "docs" without trailing slash should NOT match "docs_extra/"
        keys = await list_keys("docs", store, normalize=True)
        assert "docs/a.txt" in keys
        assert "docs_extra/b.txt" not in keys

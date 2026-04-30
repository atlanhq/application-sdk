"""Unit tests for MockStateStore (state store implementation)."""

import pytest

from application_sdk.infrastructure.state import StateStoreError
from application_sdk.testing.mocks import MockStateStore


class TestMockStateStore:
    """Tests for MockStateStore."""

    async def test_save_and_load(self) -> None:
        """Test saving and loading state by key."""
        store = MockStateStore()
        value = {"status": "running", "count": 42}

        await store.save("key1", value)
        result = await store.load("key1")

        assert result == value

    async def test_load_missing_key_returns_none(self) -> None:
        """Test that loading a missing key returns None."""
        store = MockStateStore()

        result = await store.load("nonexistent")

        assert result is None

    async def test_save_overwrites_existing_value(self) -> None:
        """Test that saving to an existing key overwrites the value."""
        store = MockStateStore()
        await store.save("key1", {"v": 1})
        await store.save("key1", {"v": 2})

        result = await store.load("key1")

        assert result == {"v": 2}

    async def test_delete_existing_key_returns_true(self) -> None:
        """Test that deleting an existing key returns True."""
        store = MockStateStore()
        await store.save("key1", {"x": 1})

        deleted = await store.delete("key1")

        assert deleted is True
        assert await store.load("key1") is None

    async def test_delete_missing_key_returns_false(self) -> None:
        """Test that deleting a missing key returns False."""
        store = MockStateStore()

        deleted = await store.delete("nonexistent")

        assert deleted is False

    async def test_list_keys_no_prefix(self) -> None:
        """Test listing all keys when no prefix is given."""
        store = MockStateStore()
        await store.save("alpha", {"a": 1})
        await store.save("beta", {"b": 2})
        await store.save("gamma", {"c": 3})

        keys = await store.list_keys()

        assert sorted(keys) == ["alpha", "beta", "gamma"]

    async def test_list_keys_with_prefix(self) -> None:
        """Test listing keys filtered by prefix."""
        store = MockStateStore()
        await store.save("app:workflow:1", {"a": 1})
        await store.save("app:workflow:2", {"b": 2})
        await store.save("app:creds:1", {"c": 3})
        await store.save("other:key", {"d": 4})

        keys = await store.list_keys(prefix="app:workflow:")

        assert sorted(keys) == ["app:workflow:1", "app:workflow:2"]

    async def test_list_keys_empty_prefix_returns_all(self) -> None:
        """Test that empty prefix returns all keys."""
        store = MockStateStore()
        await store.save("a", {})
        await store.save("b", {})

        keys = await store.list_keys(prefix="")

        assert sorted(keys) == ["a", "b"]

    async def test_list_keys_no_matches_returns_empty(self) -> None:
        """Test that a non-matching prefix returns an empty list."""
        store = MockStateStore()
        await store.save("alpha", {})

        keys = await store.list_keys(prefix="beta:")

        assert keys == []

    async def test_clear_removes_all_data(self) -> None:
        """Test that clear() removes all stored data."""
        store = MockStateStore()
        await store.save("key1", {"a": 1})
        await store.save("key2", {"b": 2})

        store.clear()

        assert await store.list_keys() == []
        assert await store.load("key1") is None

    async def test_clear_then_save_works(self) -> None:
        """Test that saving after clear works correctly."""
        store = MockStateStore()
        await store.save("key1", {"a": 1})
        store.clear()
        await store.save("key2", {"b": 2})

        keys = await store.list_keys()
        assert keys == ["key2"]


class TestStateStoreError:
    """Tests for StateStoreError."""

    def test_error_code_is_included_in_str(self) -> None:
        """Test that the error code is included in string representation."""
        err = StateStoreError("something failed")
        assert "AAF-INF-001" in str(err)

    def test_key_and_operation_included_in_str(self) -> None:
        """Test that key and operation are included in string representation."""
        err = StateStoreError("failed", key="mykey", operation="save")
        s = str(err)
        assert "key=mykey" in s
        assert "operation=save" in s

    def test_cause_included_in_str(self) -> None:
        """Test that cause is included in string representation."""
        cause = ValueError("root cause")
        err = StateStoreError("failed", cause=cause)
        assert "ValueError" in str(err)
        assert "root cause" in str(err)

    def test_default_error_code(self) -> None:
        """Test that default error code is STATE_STORE_ERROR."""
        from application_sdk.errors import STATE_STORE_ERROR

        err = StateStoreError("test")
        assert err.error_code == STATE_STORE_ERROR

    def test_custom_error_code(self) -> None:
        """Test that a custom error code can be provided."""
        from application_sdk.errors import ErrorCode

        custom = ErrorCode("TST", 99)
        err = StateStoreError("test", error_code=custom)
        assert err.error_code == custom

    @pytest.mark.parametrize(
        "key,op",
        [
            (None, None),
            ("k", None),
            (None, "save"),
            ("k", "save"),
        ],
    )
    def test_optional_fields(self, key: str | None, op: str | None) -> None:
        """Test that optional fields are handled correctly."""
        err = StateStoreError("msg", key=key, operation=op)
        s = str(err)
        assert "msg" in s
        if key:
            assert f"key={key}" in s
        if op:
            assert f"operation={op}" in s

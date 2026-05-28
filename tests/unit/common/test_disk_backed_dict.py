"""Tests for application_sdk.common.disk_backed_dict.DiskBackedDict."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

rocksdict = pytest.importorskip("rocksdict")

from application_sdk.common import disk_backed_dict as dbd_module  # noqa: E402
from application_sdk.common.disk_backed_dict import DiskBackedDict  # noqa: E402


class TestDiskBackedDict:
    def test_setitem_and_getitem(self) -> None:
        with DiskBackedDict() as d:
            d["k"] = "v"
            assert d["k"] == "v"

    def test_get_with_default(self) -> None:
        with DiskBackedDict() as d:
            assert d.get("missing") is None
            assert d.get("missing", "fallback") == "fallback"
            d["present"] = 42
            assert d.get("present") == 42

    def test_contains(self) -> None:
        with DiskBackedDict() as d:
            assert "k" not in d
            d["k"] = "v"
            assert "k" in d

    def test_delitem(self) -> None:
        with DiskBackedDict() as d:
            d["k"] = "v"
            assert "k" in d
            del d["k"]
            assert "k" not in d

    def test_delitem_missing_raises_keyerror(self) -> None:
        """Match standard dict semantics — and sqlitedict's, which the
        migration story implicitly assumes (sqlitedict raises KeyError on
        del of a missing key)."""
        with DiskBackedDict() as d:
            with pytest.raises(KeyError):
                del d["nope"]

    def test_iteration_over_keys(self) -> None:
        with DiskBackedDict() as d:
            d["a"] = 1
            d["b"] = 2
            d["c"] = 3
            assert sorted(iter(d)) == ["a", "b", "c"]
            # for-loop usage
            collected = [k for k in d]
            assert sorted(collected) == ["a", "b", "c"]

    def test_items_iteration(self) -> None:
        with DiskBackedDict() as d:
            d["a"] = 1
            d["b"] = 2
            assert sorted(d.items()) == [("a", 1), ("b", 2)]

    def test_items_empty(self) -> None:
        with DiskBackedDict() as d:
            assert list(d.items()) == []

    def test_append_to_key_new(self) -> None:
        with DiskBackedDict() as d:
            d.append_to_key("list", "first")
            assert d["list"] == ["first"]

    def test_append_to_key_existing(self) -> None:
        with DiskBackedDict() as d:
            d["list"] = ["existing"]
            d.append_to_key("list", "added")
            assert d["list"] == ["existing", "added"]

    def test_len_after_inserts(self) -> None:
        with DiskBackedDict() as d:
            d["a"] = 1
            d["b"] = 2
            d["c"] = 3
            # RocksDB's len() is an estimate, but for a small fresh store
            # it tracks inserts. Check it's >0 rather than exact.
            assert len(d) >= 1

    def test_nested_values_roundtrip(self) -> None:
        with DiskBackedDict() as d:
            d["nested"] = {"a": [1, 2, {"b": "deep"}]}
            assert d["nested"] == {"a": [1, 2, {"b": "deep"}]}

    def test_context_manager_cleanup_removes_temp_dir(self) -> None:
        with DiskBackedDict() as d:
            temp_dir = d._temp_dir
            d["k"] = "v"
            assert os.path.isdir(temp_dir)
        # After __exit__
        assert not os.path.isdir(temp_dir)

    def test_explicit_close_removes_temp_dir(self) -> None:
        d = DiskBackedDict()
        temp_dir = d._temp_dir
        d["k"] = "v"
        d.close()
        assert not os.path.isdir(temp_dir)

    def test_close_is_idempotent(self) -> None:
        """close() must be safe to call multiple times (atexit, ctx-mgr, etc.)."""
        d = DiskBackedDict()
        d["k"] = "v"
        d.close()
        # Second close — should not raise even though the db is already closed
        # and the temp dir is already gone.
        d.close()

    def test_import_error_when_rocksdict_unavailable(self) -> None:
        """Constructor must raise ImportError with an actionable message when
        rocksdict isn't installed."""
        with patch.object(dbd_module, "Rdict", None):
            with pytest.raises(ImportError, match="rocksdict is required"):
                DiskBackedDict()

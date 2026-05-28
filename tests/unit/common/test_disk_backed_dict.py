"""Tests for application_sdk.common.disk_backed_dict.DiskBackedDict."""

from __future__ import annotations

import os

import pytest

rocksdict = pytest.importorskip("rocksdict")

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

    def test_items_iteration(self) -> None:
        with DiskBackedDict() as d:
            d["a"] = 1
            d["b"] = 2
            assert sorted(d.items()) == [("a", 1), ("b", 2)]

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

    def test_complex_values_pickled(self) -> None:
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

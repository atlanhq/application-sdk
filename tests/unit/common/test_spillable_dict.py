"""Tests for application_sdk.common.spillable_dict.SpillableDict."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

rocksdict = pytest.importorskip("rocksdict")

# Imports below come after pytest.importorskip() — E402 is expected.
# CI's pinned ruff (v0.6.4) flags it; per-line noqa silences the rule on
# both these intentional post-importorskip imports.
from application_sdk.common import spillable_dict as dbd_module  # noqa: E402
from application_sdk.common.spillable_dict import SpillableDict  # noqa: E402


class TestSpillableDict:
    def test_setitem_and_getitem(self) -> None:
        with SpillableDict() as d:
            d["k"] = "v"
            assert d["k"] == "v"

    def test_get_with_default(self) -> None:
        with SpillableDict() as d:
            assert d.get("missing") is None
            assert d.get("missing", "fallback") == "fallback"
            d["present"] = 42
            assert d.get("present") == 42

    def test_contains(self) -> None:
        with SpillableDict() as d:
            assert "k" not in d
            d["k"] = "v"
            assert "k" in d

    def test_delitem(self) -> None:
        with SpillableDict() as d:
            d["k"] = "v"
            assert "k" in d
            del d["k"]
            assert "k" not in d

    def test_delitem_missing_raises_keyerror(self) -> None:
        """Match standard dict semantics — and sqlitedict's, which the
        migration story implicitly assumes (sqlitedict raises KeyError on
        del of a missing key)."""
        with SpillableDict() as d, pytest.raises(KeyError):
            del d["nope"]

    def test_delitem_under_load_exercises_bloom_recovery(self) -> None:
        """Insert N keys + probe nearby never-inserted keys to force the
        bloom-filter false-positive branch in __delitem__ (the explicit
        `or key not in self._db` recovery clause). At default bloom config
        (~1% FP rate) probing 5K nearby keys should hit several FPs — even
        if zero hits, the test still validates that all 5K KeyErrors are
        raised correctly (i.e., the recovery clause stays correct in
        either branch)."""
        with SpillableDict() as d:
            for i in range(1000):
                d[f"key_{i}"] = i
            # Probe a parallel keyspace that was never inserted. Each
            # del must raise KeyError regardless of which branch fires.
            for i in range(5000):
                with pytest.raises(KeyError):
                    del d[f"missing_{i}"]

    def test_iteration_over_keys(self) -> None:
        with SpillableDict() as d:
            d["a"] = 1
            d["b"] = 2
            d["c"] = 3
            assert sorted(iter(d)) == ["a", "b", "c"]
            # for-loop usage
            collected = [k for k in d]
            assert sorted(collected) == ["a", "b", "c"]

    def test_items_iteration(self) -> None:
        with SpillableDict() as d:
            d["a"] = 1
            d["b"] = 2
            assert sorted(d.items()) == [("a", 1), ("b", 2)]

    def test_items_empty(self) -> None:
        with SpillableDict() as d:
            assert list(d.items()) == []

    def test_append_to_key_new(self) -> None:
        with SpillableDict() as d:
            d.append_to_key("list", "first")
            assert d["list"] == ["first"]

    def test_append_to_key_existing(self) -> None:
        with SpillableDict() as d:
            d["list"] = ["existing"]
            d.append_to_key("list", "added")
            assert d["list"] == ["existing", "added"]

    def test_len_is_exact(self) -> None:
        """__len__ honors MutableMapping's exact-len contract."""
        with SpillableDict() as d:
            assert len(d) == 0
            d["a"] = 1
            d["b"] = 2
            d["c"] = 3
            assert len(d) == 3
            del d["b"]
            assert len(d) == 2

    def test_bool_empty(self) -> None:
        """bool(d) on empty dict must be False — relies on __len__ exactness."""
        with SpillableDict() as d:
            assert not bool(d)
            d["k"] = "v"
            assert bool(d)

    def test_approximate_size_is_estimate(self) -> None:
        """approximate_size() returns an integer; doesn't need to match exactly."""
        with SpillableDict() as d:
            d["a"] = 1
            d["b"] = 2
            assert isinstance(d.approximate_size(), int)
            assert d.approximate_size() >= 0

    def test_nested_values_roundtrip(self) -> None:
        with SpillableDict() as d:
            d["nested"] = {"a": [1, 2, {"b": "deep"}]}
            assert d["nested"] == {"a": [1, 2, {"b": "deep"}]}

    def test_context_manager_cleanup_removes_temp_dir(self) -> None:
        with SpillableDict() as d:
            temp_dir = d._temp_dir
            d["k"] = "v"
            assert os.path.isdir(temp_dir)
        # After __exit__
        assert not os.path.isdir(temp_dir)

    def test_explicit_close_removes_temp_dir(self) -> None:
        d = SpillableDict()
        temp_dir = d._temp_dir
        d["k"] = "v"
        d.close()
        assert not os.path.isdir(temp_dir)

    def test_close_is_idempotent(self) -> None:
        """close() must be safe to call multiple times (atexit, ctx-mgr, etc.)."""
        d = SpillableDict()
        d["k"] = "v"
        d.close()
        # Second close — should not raise even though the db is already closed
        # and the temp dir is already gone.
        d.close()

    def test_import_error_when_rocksdict_unavailable(self) -> None:
        """Constructor must raise ImportError with an actionable message when
        rocksdict isn't installed."""
        with (
            patch.object(dbd_module, "Rdict", None),
            pytest.raises(ImportError, match="rocksdict is required"),
        ):
            SpillableDict()

    def test_is_mutablemapping(self) -> None:
        """Inheriting from MutableMapping gives pop/update/setdefault/keys/
        values/clear for free — verify the registration."""
        from collections.abc import MutableMapping

        d = SpillableDict()
        try:
            assert isinstance(d, MutableMapping)
        finally:
            d.close()

    def test_mutablemapping_pop(self) -> None:
        with SpillableDict() as d:
            d["k"] = "v"
            assert d.pop("k") == "v"
            assert "k" not in d
            # missing key → KeyError unless default given
            with pytest.raises(KeyError):
                d.pop("missing")
            assert d.pop("missing", "default") == "default"

    def test_mutablemapping_setdefault(self) -> None:
        with SpillableDict() as d:
            assert d.setdefault("k", "new") == "new"
            assert d["k"] == "new"
            assert d.setdefault("k", "ignored") == "new"  # existing wins

    def test_mutablemapping_update(self) -> None:
        with SpillableDict() as d:
            d.update({"a": 1, "b": 2})
            assert d["a"] == 1
            assert d["b"] == 2

    def test_mutablemapping_keys_values(self) -> None:
        with SpillableDict() as d:
            d["a"] = 1
            d["b"] = 2
            assert sorted(d.keys()) == ["a", "b"]
            assert sorted(d.values()) == [1, 2]

    def test_mutablemapping_clear(self) -> None:
        with SpillableDict() as d:
            d["a"] = 1
            d["b"] = 2
            d.clear()
            assert "a" not in d
            assert "b" not in d

    def test_contains_accepts_non_str(self) -> None:
        """__contains__ signature is `key: object` (protocol). Passing a
        non-str shouldn't raise — should just return False if the key
        wasn't stored, regardless of type."""
        with SpillableDict() as d:
            d["k"] = "v"
            # These should not raise — just return False
            assert (123 in d) is False
            assert (None in d) is False  # type: ignore[operator]
            assert ([1, 2] in d) is False  # unhashable-style probe
            assert (object() in d) is False


class TestSpillableDictKeyTypeGuards:
    """Defensive guards on the four entry points so the dict behaves like
    standard dict/sqlitedict for None and non-primitive keys.

    rocksdict accepts (str, int, float, bool, bytes) as keys; anything else
    raises internally with a TypeError. Connector callers pass None for
    optional join keys (e.g. dbtcore source rows without an associated
    job), which previously crashed the whole workflow. These guards
    return safe defaults instead.

    Mirrors the behaviour the ``atlan-dbt-app/.../RobustSpillableDict``
    subclass implemented before being folded back into the SDK
    (atlan-dbt-app PR #173 → application-sdk follow-up).
    """

    def test_get_none_returns_default(self) -> None:
        with SpillableDict() as d:
            d["k"] = "v"
            assert d.get(None) is None  # type: ignore[arg-type]
            assert d.get(None, "fallback") == "fallback"  # type: ignore[arg-type]

    def test_get_unsupported_type_returns_default(self) -> None:
        with SpillableDict() as d:
            d["k"] = "v"
            assert d.get([1, 2, 3], "miss") == "miss"  # type: ignore[arg-type]
            assert d.get(object(), "miss") == "miss"  # type: ignore[arg-type]
            assert d.get({"unhashable": "but valid arg"}, "miss") == "miss"  # type: ignore[arg-type]

    def test_getitem_unsupported_type_raises_keyerror(self) -> None:
        with SpillableDict() as d:
            with pytest.raises(KeyError):
                _ = d[None]  # type: ignore[index]
            with pytest.raises(KeyError):
                _ = d[[1, 2]]  # type: ignore[index]
            with pytest.raises(KeyError):
                _ = d[object()]  # type: ignore[index]

    def test_setitem_none_is_silent_noop(self) -> None:
        """Connector code paths set d[jobId] = info even when jobId is
        None (job-less source rows). Silently drop the write rather than
        force every caller to guard."""
        with SpillableDict() as d:
            d[None] = "ignored"  # type: ignore[index]
            assert (None in d) is False  # type: ignore[operator]
            # The store stays empty — the None write didn't sneak under
            # some other key.
            assert len(d) == 0
            # A real key after the noop still works.
            d["k"] = "v"
            assert d["k"] == "v"
            assert len(d) == 1

    def test_setitem_other_non_primitive_raises_loudly(self) -> None:
        """Anything other than None must surface rocksdict's TypeError —
        we don't silently drop list/dict/object writes, otherwise a caller
        bug (forgetting to stringify a key) becomes silent data loss."""
        with SpillableDict() as d:
            with pytest.raises((TypeError, Exception)):
                d[[1, 2]] = "loud"  # type: ignore[index]
            with pytest.raises((TypeError, Exception)):
                d[object()] = "loud"  # type: ignore[index]

    def test_delitem_none_raises_keyerror(self) -> None:
        with SpillableDict() as d, pytest.raises(KeyError):
            del d[None]  # type: ignore[arg-type]

    def test_delitem_unsupported_type_raises_keyerror(self) -> None:
        with SpillableDict() as d:
            with pytest.raises(KeyError):
                del d[[1, 2]]  # type: ignore[arg-type]
            with pytest.raises(KeyError):
                del d[object()]  # type: ignore[arg-type]

    def test_all_supported_primitive_key_types_round_trip(self) -> None:
        """All five RocksDB-supported types work as keys end-to-end."""
        with SpillableDict() as d:
            d["s"] = "via-str"
            d[42] = "via-int"
            d[3.14] = "via-float"
            d[True] = "via-bool"
            d[b"bk"] = "via-bytes"
            assert d.get("s") == "via-str"
            assert d.get(42) == "via-int"
            assert d.get(3.14) == "via-float"
            assert d.get(True) == "via-bool"
            assert d.get(b"bk") == "via-bytes"
            for k in ("s", 42, 3.14, True, b"bk"):
                assert k in d

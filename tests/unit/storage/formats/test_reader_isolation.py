"""Regression tests for BLDX-1167 — `Reader._downloaded_files` and `_is_closed`
must be per-instance state, not shared via class-level mutable defaults.

The fix moves both attributes into `Reader.__init__` and updates the in-tree
subclasses (`JsonFileReader`, `ParquetFileReader`) to call `super().__init__()`
so the contract documented on the base class is honoured.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from application_sdk.storage.formats import Reader


class _BareReader(Reader):
    """Minimal concrete `Reader` for testing the base-class contract.

    Mirrors the pattern an out-of-tree subclass author would follow: only the
    abstract methods are implemented; nothing is set in `__init__`. The fix
    relies on `Reader.__init__` running so `_downloaded_files` and
    `_is_closed` end up as per-instance state.
    """

    async def read(self) -> Any:  # pragma: no cover — not exercised here
        return None

    async def read_batches(self) -> AsyncIterator[Any]:  # pragma: no cover
        if False:
            yield None  # noqa: PLR1704


def test_reader_downloaded_files_is_per_instance() -> None:
    """Two `Reader` instances must not share `_downloaded_files`.

    Before BLDX-1167, `_downloaded_files: List[str] = []` lived on the class,
    so any `r._downloaded_files.append(...)` mutated the shared list. After
    the fix, each instance gets its own list via `Reader.__init__`.
    """
    a = _BareReader()
    b = _BareReader()

    a._downloaded_files.append("/tmp/from_a")

    assert "/tmp/from_a" not in b._downloaded_files, (
        "Reader._downloaded_files leaked across instances — class-level "
        "mutable default regressed (BLDX-1167)"
    )
    assert a._downloaded_files == ["/tmp/from_a"]


def test_reader_is_closed_is_per_instance() -> None:
    """`_is_closed` must also be per-instance — flipping one Reader closed
    must not appear closed on a sibling Reader."""
    a = _BareReader()
    b = _BareReader()

    a._is_closed = True
    assert b._is_closed is False


def test_reader_default_state_is_empty_and_open() -> None:
    """Sanity: a fresh Reader starts with an empty downloads list and open
    state. Locks the contract documented on `Reader.__init__`."""
    r = _BareReader()
    assert r._downloaded_files == []
    assert r._is_closed is False


def test_concrete_subclasses_call_super_init(tmp_path) -> None:
    """`JsonFileReader` and `ParquetFileReader` must call `super().__init__()`
    so the base-class contract is enforced.

    Constructing each subclass should result in non-shared `_downloaded_files`
    lists. Before the BLDX-1167 fix the duplicate `self._downloaded_files = []`
    in subclass `__init__` masked the bug for in-tree subclasses; this test
    locks the inheritance contract regardless.
    """
    from application_sdk.storage.formats.json import JsonFileReader
    from application_sdk.storage.formats.parquet import ParquetFileReader

    j_path = tmp_path / "data.json"
    j_path.write_text("{}")
    p_path = tmp_path / "data.parquet"
    p_path.write_bytes(b"")

    j1 = JsonFileReader(path=str(j_path))
    j2 = JsonFileReader(path=str(j_path))
    j1._downloaded_files.append("/tmp/from_j1")
    assert "/tmp/from_j1" not in j2._downloaded_files

    p1 = ParquetFileReader(path=str(p_path))
    p2 = ParquetFileReader(path=str(p_path))
    p1._downloaded_files.append("/tmp/from_p1")
    assert "/tmp/from_p1" not in p2._downloaded_files


# Avoid `unused asyncio import` warning when the abstract method body is empty.
_ = asyncio
_ = pytest

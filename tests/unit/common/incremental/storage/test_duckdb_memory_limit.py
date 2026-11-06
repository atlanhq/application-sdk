"""DuckDB's default memory limit must fit inside the container it runs in.

A production RCA: a fixed 2GB DuckDB limit on a worker with a 1.95 GiB
container limit, already 80%+ used, let a snapshot scan grow until the
kernel OOM-killed the worker instead of spilling to disk.
"""

from __future__ import annotations

import pytest

from application_sdk.common.incremental.storage import duckdb_utils
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.observability import cgroup

pytest.importorskip("duckdb")

_SMALL_CONTAINER = 2_093_796_556  # 1.95 GiB, the limit the failing worker logged
# DuckDB reports sizes over 1 GiB truncated to one decimal place.
_DISPLAY_ROUNDING = 0.1 * 1024**3


def _effective_limit_bytes(manager: DuckDBConnectionManager) -> int:
    (value,) = manager.connection.execute(
        "SELECT value FROM duckdb_settings() WHERE name = 'memory_limit'"
    ).fetchone()
    number, unit = str(value).split()
    scale = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return int(float(number) * scale[unit])


@pytest.fixture
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_utils, "DUCKDB_MEMORY_LIMIT_OVERRIDE", "", raising=False)


def test_default_limit_fits_small_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: _SMALL_CONTAINER)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        limit = _effective_limit_bytes(manager)
    assert limit <= 0.4 * _SMALL_CONTAINER + 1024**2


def test_default_limit_without_container_limit_is_2gb(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: None)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        limit = _effective_limit_bytes(manager)
    assert abs(limit - 2 * 1000**3) <= _DISPLAY_ROUNDING


def test_default_limit_capped_at_2gb_on_large_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: 16 * 1024**3)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        limit = _effective_limit_bytes(manager)
    assert abs(limit - 2 * 1000**3) <= _DISPLAY_ROUNDING


def test_default_limit_floor_on_tiny_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: 128 * 1024**2)
    assert duckdb_utils.default_duckdb_memory_limit() == "256MiB"


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(duckdb_utils, "DUCKDB_MEMORY_LIMIT_OVERRIDE", "512MiB")
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: _SMALL_CONTAINER)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        assert _effective_limit_bytes(manager) == 512 * 1024**2


def test_explicit_limit_is_respected(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: _SMALL_CONTAINER)
    with DuckDBConnectionManager(
        base_path=str(tmp_path), memory_limit="1GB"
    ) as manager:
        assert abs(_effective_limit_bytes(manager) - 1000**3) <= _DISPLAY_ROUNDING

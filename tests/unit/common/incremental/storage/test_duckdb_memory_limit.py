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
    # Usage unknown unless a test says otherwise: the whole limit counts as free.
    monkeypatch.setattr(cgroup, "memory_usage_bytes", lambda: None)


def test_default_limit_fits_small_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: _SMALL_CONTAINER)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        limit = _effective_limit_bytes(manager)
    assert limit <= 0.5 * _SMALL_CONTAINER + 1024**2


def test_default_limit_is_sized_from_free_memory_not_the_whole_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    """The motivating case: the worker already holds 80% of a 1.95 GiB
    container, so ~399 MiB is free. DuckDB must get less than that, not 40%
    of the whole limit (~798 MiB), or it OOMs the container before spilling."""
    used = int(0.8 * _SMALL_CONTAINER)
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: _SMALL_CONTAINER)
    monkeypatch.setattr(cgroup, "memory_usage_bytes", lambda: used)
    with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
        limit = _effective_limit_bytes(manager)
    assert limit < _SMALL_CONTAINER - used


def test_concurrent_managers_share_the_free_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    """Each open manager may still grow, so a second one gets a share, not
    its own full budget, and the two together stay inside the free memory."""
    container = 8 * 1024**3
    used = 4 * 1024**3
    monkeypatch.setattr(cgroup, "memory_limit_bytes", lambda: container)
    monkeypatch.setattr(cgroup, "memory_usage_bytes", lambda: used)
    with DuckDBConnectionManager(base_path=str(tmp_path / "a")) as first:
        with DuckDBConnectionManager(base_path=str(tmp_path / "b")) as second:
            a = _effective_limit_bytes(first)
            b = _effective_limit_bytes(second)
    assert b < a
    assert a + b <= container - used
    # Both closed: the next one gets the whole share again.
    assert duckdb_utils.default_duckdb_memory_limit(duckdb_utils._open_managers) == (
        f"{min(2 * 1000**3, (container - used) // 2) // 1024**2}MiB"
    )


def test_a_manager_that_fails_to_open_releases_its_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_override
) -> None:
    before = duckdb_utils._open_managers

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cannot open")

    monkeypatch.setattr(duckdb_utils.duckdb, "connect", boom)
    with pytest.raises(RuntimeError):
        DuckDBConnectionManager(base_path=str(tmp_path))
    assert duckdb_utils._open_managers == before


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

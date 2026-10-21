"""Adaptive multipart sizing and per-writer memory bounds."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.storage import ops
from application_sdk.storage._budget import ByteBudget

MIB = 1024 * 1024


@pytest.mark.parametrize("old_parts", [1194, 1244, 1263, 1280])
async def test_large_upload_bounds_parts(old_parts, monkeypatch):
    """Exercise the public upload path without allocating a multi-GiB payload."""
    monkeypatch.setattr(
        "application_sdk.constants.STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN", False
    )
    monkeypatch.setattr(
        "application_sdk.constants.STORAGE_UPLOAD_PART_SIZE_BYTES", 8 * MIB
    )
    file_size = old_parts * 8 * MIB
    path = MagicMock()
    path.stat.return_value = SimpleNamespace(st_size=file_size)
    path.open.return_value.__enter__.return_value = io.BytesIO(b"payload")
    writer = MagicMock()
    writer.__aenter__ = AsyncMock(return_value=SimpleNamespace(write=AsyncMock()))
    writer.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(ops, "Path", return_value=path),
        patch.object(
            ops.obstore, "open_writer_async", return_value=writer
        ) as open_writer,
    ):
        await ops.upload_file(
            "artifacts/column.json",
            "unused.bin",
            MagicMock(),
            normalize=False,
            verify=False,
            compute_hash=False,
        )
    part_size = open_writer.call_args.kwargs["buffer_size"]
    assert (file_size + part_size - 1) // part_size <= 128


async def test_large_parts_cap_concurrency_to_budget(monkeypatch):
    """Reservation accounting must match the concurrency actually given to obstore."""
    monkeypatch.setattr("application_sdk.constants.STORAGE_UPLOAD_MAX_CONCURRENCY", 12)
    budget = ByteBudget(256 * MIB)
    monkeypatch.setattr(ops, "transfer_budget", lambda: budget)
    path = MagicMock()
    path.open.return_value.__enter__.return_value = io.BytesIO(b"")
    writer = MagicMock()
    writer.__aenter__ = AsyncMock()
    writer.__aexit__ = AsyncMock(return_value=False)
    with patch.object(
        ops.obstore, "open_writer_async", return_value=writer
    ) as open_writer:
        await ops._upload_once(
            MagicMock(),
            "artifacts/data.bin",
            path,
            effective_chunk=64 * MIB,
            put_attributes=None,
            compute_hash=False,
            started=0,
            file_size=10 * 1024**3,
        )
    concurrency = open_writer.call_args.kwargs["max_concurrency"]
    assert concurrency == 2  # two in-flight parts, one filling, one being read
    assert (concurrency + 2) * 64 * MIB <= budget.capacity
    assert budget.peak == (concurrency + 2) * 64 * MIB
    assert budget.used == 0


@pytest.mark.parametrize("file_size", [0, 1024, 128 * 8 * MIB])
def test_small_files_keep_requested_size(file_size, monkeypatch):
    """The new default must not inflate parts below the target count."""
    monkeypatch.setattr(
        "application_sdk.constants.STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN", False
    )
    assert ops._compute_part_size(file_size, 8 * MIB) == 8 * MIB


def test_deployment_part_override_keeps_s3_safety_margin(monkeypatch):
    """Explicit operator sizing retains the old 9,900-part safeguard."""
    monkeypatch.setattr(
        "application_sdk.constants.STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN", True
    )
    assert ops._compute_part_size(10 * 1024**3, 8 * MIB) == 8 * MIB
    size = 100_000 * 8 * MIB
    part_size = ops._compute_part_size(size, 8 * MIB)
    assert (size + part_size - 1) // part_size <= 9900


def test_part_size_uses_integer_ceiling_for_large_files(monkeypatch):
    """Rounding must not introduce a 129th part at an exact boundary."""
    monkeypatch.setattr(
        "application_sdk.constants.STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN", False
    )
    size = 128 * 80 * MIB + 1
    part_size = ops._compute_part_size(size, 8 * MIB)
    assert (size + part_size - 1) // part_size <= 128

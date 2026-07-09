"""Fixtures and helpers for the opt-in storage load tests.

These tests move REAL bytes at a configurable size (default 1 GiB, 50 GiB for
incident-class validation) through the full public transfer stack against an
obstore ``LocalStore`` — no mocks on the data path, no cloud credentials.

Size is controlled by ``ATLAN_LOAD_TEST_SIZE_MIB`` (see README.md). Disk
requirement is roughly 2× the configured size per test (store copy + local
copy); set ``TMPDIR`` to a volume with enough space for large runs.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from application_sdk.storage.factory import create_local_store

#: Test payload size. 1 GiB default keeps a local run in minutes; set 51200
#: (50 GiB) to replay the incident class end-to-end.
LOAD_SIZE_MIB = int(os.getenv("ATLAN_LOAD_TEST_SIZE_MIB", "1024"))
LOAD_SIZE_BYTES = LOAD_SIZE_MIB * 1024 * 1024

_MIB = 1024 * 1024
_BASE_BLOCK = bytes(range(256)) * 4096  # 1 MiB


@pytest.fixture
def local_store(tmp_path):
    """Real obstore ``LocalStore`` rooted in an isolated temp directory."""
    return create_local_store(tmp_path / "objectstore")


@pytest.fixture(autouse=True)
def _reclaim_disk(tmp_path):
    """Delete this test's payloads as soon as it finishes.

    pytest retains every test's tmp dir for the whole session; at 50 GiB per
    scenario that would accumulate ~400 GB across the suite. Reclaiming per
    test caps peak usage at one scenario's footprint (~2× the payload).
    """
    yield
    import shutil

    for child in tmp_path.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(
            missing_ok=True
        )


def write_source_file(path: Path, size_bytes: int) -> str:
    """Stream a deterministic *size_bytes* file to *path*; return its sha256.

    Each MiB block is prefixed with its index so no two chunks are identical —
    a byte written at the wrong offset can never go undetected.
    """
    h = hashlib.sha256()
    remaining = size_bytes
    idx = 0
    with path.open("wb") as fh:
        while remaining > 0:
            block = idx.to_bytes(8, "big") + _BASE_BLOCK[8:]
            block = block[: min(_MIB, remaining)]
            fh.write(block)
            h.update(block)
            remaining -= len(block)
            idx += 1
    return h.hexdigest()


def report(label: str, size_bytes: int, elapsed_s: float) -> None:
    """Print a one-line throughput summary (visible with ``pytest -s``)."""
    mib = size_bytes / _MIB
    mibps = mib / elapsed_s if elapsed_s > 0 else float("inf")
    print(  # noqa: T201 — operator-facing summary; load tests run with `pytest -s`
        f"\n[load] {label}: {mib:,.0f} MiB in {elapsed_s:,.1f}s → {mibps:,.1f} MiB/s"
    )


class Stopwatch:
    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.monotonic() - self.t0
        return False

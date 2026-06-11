"""v2 → v3 path-normalization matrix against a real local objectstore.

The v2 SDK accepted workflow staging paths (``./local/tmp/artifacts/...``) as
object-store keys and normalised them via ``ObjectStore.as_store_key()``.  In
v3 the same normalisation lives in ``normalize_key`` and is applied by every
public storage helper.  This was a recent prod-fix area (the v2→v3
objectstore migration gaps), so this matrix pins the contract:

    For every public storage operation, a v2-style staging-path key and the
    equivalent v3 store key MUST produce identical outcomes — including full
    interoperability (write with one shape, read/delete with the other).

All tests use the ``staging`` fixture, which points ``TEMPORARY_PATH`` at a
temp directory so v2-style paths can be built hermetically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.storage.batch import delete_prefix, download_prefix, list_keys
from application_sdk.storage.ops import (
    delete,
    download_file,
    exists,
    get_file_size,
    normalize_key,
    upload_file,
)

V3_KEY = "artifacts/run-1/data.txt"
V3_PREFIX = "artifacts/run-1"
CONTENT = "v2-v3 parity"


def _v2_key(staging: Path, v3_key: str) -> str:
    """Build the v2-style staging-path spelling of *v3_key*."""
    return str(staging / v3_key)


def _key(style: str, staging: Path, v3_key: str) -> str:
    return v3_key if style == "v3" else _v2_key(staging, v3_key)


# ------------------------------------------------------------------
# Same-shape matrix: each op behaves identically for both key shapes
# ------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("style", ["v3", "v2"])
async def test_full_lifecycle_identical_for_both_key_shapes(
    style, local_store, staging, tmp_path
):
    """upload → exists → size → list → download → delete via one key shape."""
    key = _key(style, staging, V3_KEY)
    prefix = _key(style, staging, V3_PREFIX)

    src = tmp_path / "src.txt"
    src.write_text(CONTENT)

    up_sha = await upload_file(key, src, local_store)
    assert up_sha is not None

    # Regardless of the input shape, the stored key is the normalised v3 key.
    assert await exists(key, local_store) is True
    assert await get_file_size(key, local_store) == len(CONTENT)
    assert await list_keys(prefix, local_store) == [V3_KEY]

    dest = tmp_path / "dest.txt"
    dl_sha = await download_file(key, dest, local_store, compute_hash=True)
    assert dest.read_text() == CONTENT
    assert dl_sha == up_sha

    assert await delete(key, local_store) is True
    assert await exists(key, local_store) is False


# ------------------------------------------------------------------
# Cross-shape matrix: write with one shape, operate with the other
# ------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("write_style", "read_style"),
    [("v2", "v3"), ("v3", "v2")],
)
async def test_cross_shape_interoperability(
    write_style, read_style, local_store, staging, tmp_path
):
    """An object written via one key shape is fully reachable via the other."""
    write_key = _key(write_style, staging, V3_KEY)
    read_key = _key(read_style, staging, V3_KEY)
    read_prefix = _key(read_style, staging, V3_PREFIX)

    src = tmp_path / "src.txt"
    src.write_text(CONTENT)
    await upload_file(write_key, src, local_store)

    assert await exists(read_key, local_store) is True
    assert await list_keys(read_prefix, local_store) == [V3_KEY]

    dest = tmp_path / "dest.txt"
    await download_file(read_key, dest, local_store)
    assert dest.read_text() == CONTENT

    assert await delete(read_key, local_store) is True
    # Once deleted via either shape, it is gone for both.
    assert await exists(write_key, local_store) is False
    assert await exists(read_key, local_store) is False


@pytest.mark.integration
@pytest.mark.parametrize("style", ["v3", "v2"])
async def test_prefix_batch_ops_identical_for_both_shapes(
    style, local_store, staging, tmp_path
):
    """download_prefix / delete_prefix accept both prefix shapes identically."""
    prefix = _key(style, staging, V3_PREFIX)

    for name in ("a.txt", "b.txt"):
        src = tmp_path / name
        src.write_text(name)
        await upload_file(f"{V3_PREFIX}/{name}", src, local_store)

    dest = tmp_path / f"dl-{style}"
    paths = await download_prefix(prefix, dest, local_store)
    # Downloaded layout always mirrors the normalised v3 keys.
    assert sorted(Path(p).relative_to(dest).as_posix() for p in paths) == [
        f"{V3_PREFIX}/a.txt",
        f"{V3_PREFIX}/b.txt",
    ]

    assert await delete_prefix(prefix, local_store) == 2
    assert await list_keys(V3_PREFIX, local_store) == []


# ------------------------------------------------------------------
# normalize_key is the single source of truth for the mapping
# ------------------------------------------------------------------


@pytest.mark.integration
async def test_normalize_key_maps_v2_paths_onto_v3_keys(staging):
    """The documented v2 → v3 key mapping, pinned explicitly."""
    assert normalize_key(_v2_key(staging, V3_KEY)) == V3_KEY
    assert normalize_key(V3_KEY) == V3_KEY
    # The staging root itself maps to the store root.
    assert normalize_key(str(staging)) == ""
    # Leading slash on an absolute non-staging path is stripped to a relative key.
    assert normalize_key("/data/output.parquet") == "data/output.parquet"


@pytest.mark.integration
async def test_normalize_false_bypasses_v2_mapping(local_store, staging, tmp_path):
    """normalize=False stores the raw key — v2 paths are NOT remapped then.

    This pins the escape hatch: callers that pass ``normalize=False`` get the
    exact key they supplied, so a v2-style path stays a literal key (stripped
    of its leading slash by the store backend, not by the SDK).
    """
    src = tmp_path / "raw.txt"
    src.write_text("raw")

    raw_key = "artifacts/raw-key.txt"
    await upload_file(raw_key, src, local_store, normalize=False)
    assert await exists(raw_key, local_store, normalize=False) is True

    # The v2 spelling of that key normalises to the same key, so the
    # normalised read still finds the object written with normalize=False.
    assert await exists(_v2_key(staging, raw_key), local_store) is True

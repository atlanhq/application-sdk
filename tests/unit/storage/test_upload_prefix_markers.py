"""Directory re-uploads must not treat local integrity markers as data."""

from __future__ import annotations

import hashlib
from pathlib import Path

import obstore
import pytest

from application_sdk.storage.batch import download_prefix, upload_prefix
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes


@pytest.mark.parametrize("prune", [False, True])
@pytest.mark.parametrize("retain_local_copy", [False, True])
async def test_rewritten_artifact_ignores_stale_local_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prune: bool,
    retain_local_copy: bool,
) -> None:
    monkeypatch.setattr("application_sdk.constants.TEMPORARY_PATH", str(tmp_path))
    store = create_memory_store()
    source = tmp_path / "raw" / "embedded_datasources"
    source.mkdir(parents=True)
    artifact = source / "result-0.json"
    original = b'{"items": [1, 2]}'
    rewritten = b'{"items": [1]}'
    artifact.write_bytes(original)
    await upload_prefix(tmp_path / "raw", "remote", store, normalize=False)

    # A previous transfer left a local marker; processing rewrites only the data.
    marker = artifact.with_name(artifact.name + ".sha256")
    stale_digest = hashlib.sha256(original).hexdigest()
    marker.write_text(stale_digest)
    artifact.write_bytes(rewritten)

    uploaded = await upload_prefix(
        tmp_path / "raw",
        "remote",
        store,
        normalize=False,
        max_concurrency=1,
        prune=prune,
        retain_local_copy=retain_local_copy,
    )

    key = "remote/embedded_datasources/result-0.json"
    assert uploaded == [key]
    assert await _get_bytes(key, store, normalize=False) == rewritten
    assert await _get_bytes(key + ".sha256", store, normalize=False) == (
        hashlib.sha256(rewritten).hexdigest().encode()
    )
    stored_keys = [item["path"] for batch in obstore.list(store) for item in batch]
    assert sorted(stored_keys) == [key, key + ".sha256"]

    destination = tmp_path / "download"
    await download_prefix(
        "remote/", destination, store, normalize=False, strip_prefix=True
    )
    assert (destination / "embedded_datasources" / "result-0.json").read_bytes() == (
        rewritten
    )
    assert marker.read_text() == stale_digest
    assert artifact.exists() is retain_local_copy


async def test_marker_only_directory_uploads_no_objects(tmp_path: Path) -> None:
    store = create_memory_store()
    (tmp_path / "orphan.json.sha256").write_text("0" * 64)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "orphan.json.sha256.sha256").write_text("1" * 64)

    uploaded = await upload_prefix(tmp_path, "", store, normalize=False)

    assert uploaded == []
    assert list(obstore.list(store)) == []

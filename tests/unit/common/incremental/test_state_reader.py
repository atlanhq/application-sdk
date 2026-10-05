"""Tests for the deprecated ``download_current_state`` shim.

It now probes the committed snapshot and materializes it into the connection's
fixed directory. Run against a real in-memory store: what matters is which
keys land where, not which helper was called.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental import helpers
from application_sdk.common.incremental.state.state_reader import download_current_state
from application_sdk.common.incremental.state.store import CurrentStateStore
from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import _put

CONN = "default/oracle/123"
APP = "oracle"
PREFIX = "persistent-artifacts/apps/oracle/connection/123/current-state"


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path))
    return tmp_path


async def _download():
    with pytest.warns(DeprecationWarning, match="CurrentStateStore.probe"):
        return await download_current_state(
            connection_qualified_name=CONN, application_name=APP
        )


class TestDownloadCurrentState:
    async def test_first_run_returns_not_exists(self, memory_store, staging) -> None:
        state_dir, s3_prefix, exists, json_count = await _download()

        assert s3_prefix == PREFIX
        assert exists is False
        assert json_count == 0
        assert state_dir.is_dir()

    async def test_state_lands_unnested_under_current_state_dir(
        self, memory_store, staging
    ) -> None:
        """Downloaded state must land at ``<current-state>/<entity>/`` (FND-340)."""
        await _put(f"{PREFIX}/table/chunk-0.json", b'{"t": 1}', memory_store)
        await _put(f"{PREFIX}/column/chunk-0.json", b'{"c": 1}', memory_store)

        state_dir, _, exists, json_count = await _download()

        assert exists is True
        assert json_count == 2
        assert (state_dir / "table" / "chunk-0.json").read_bytes() == b'{"t": 1}'
        assert (state_dir / "column" / "chunk-0.json").read_bytes() == b'{"c": 1}'
        assert not (state_dir / "persistent-artifacts").exists()

    async def test_only_the_committed_snapshot_is_downloaded(
        self, memory_store, staging, tmp_path
    ) -> None:
        """Keys outside the manifest — a failed commit's partial upload — and
        stale local files from an earlier download are not part of the result."""
        build = tmp_path / "build"
        (build / "table").mkdir(parents=True)
        (build / "table" / "chunk-0.json").write_text('{"t": 1}\n')
        await CurrentStateStore(PREFIX, memory_store).commit(build, "run-1")
        await _put(f"{PREFIX}/table/0123456789ab--chunk-9.json", b"{}", memory_store)

        stale = helpers.get_persistent_artifacts_path(CONN, "current-state", APP)
        (stale / "column").mkdir(parents=True)
        (stale / "column" / "stale.json").write_text("{}")

        state_dir, _, exists, json_count = await _download()

        assert exists is True
        assert json_count == 1
        files = sorted(
            p.relative_to(state_dir).as_posix()
            for p in state_dir.rglob("*.json")
            if ".sdk-" not in p.as_posix()
        )
        assert len(files) == 1 and files[0].endswith("--chunk-0.json")

    async def test_storage_error_propagates(self, memory_store, staging) -> None:
        """A store outage raises so the task retries, rather than reading as
        "no state" and turning the run into a full extraction."""
        with patch(
            "application_sdk.common.incremental.state.store.list_data_objects",
            new=AsyncMock(side_effect=StorageError("injected: store unavailable")),
        ):
            with pytest.raises(StorageError, match="store unavailable"):
                await _download()

"""Telemetry upload to the deployment store follows whether its Dapr component exists."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME, LOG_FILE_NAME
from application_sdk.observability.observability import AtlanObservability
from tests.unit.conftest import write_component

_KEY = "artifacts/apps/observability/logs/batch.json.gz"


class _ConcreteObs(AtlanObservability):
    def process_record(self, record: Any) -> dict[str, Any]:
        return record

    def export_record(self, record: Any) -> None:
        pass


@pytest.fixture
def obs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    components = tmp_path / "components"
    components.mkdir()
    monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(components))
    AtlanObservability._reset_for_testing()
    yield _ConcreteObs(
        batch_size=100,
        flush_interval=60,
        retention_days=7,
        cleanup_enabled=False,
        data_dir=str(tmp_path / "data"),
        file_name=LOG_FILE_NAME,
    )
    AtlanObservability._reset_for_testing()


@pytest.fixture
def batch_file(tmp_path: Path) -> Path:
    path = tmp_path / "batch.json.gz"
    path.write_bytes(b"telemetry")
    return path


async def test_upload_is_skipped_quietly_without_the_component(
    obs: _ConcreteObs, batch_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        await obs._upload_and_delete(str(batch_file), _KEY)
        await obs._upload_and_delete(str(batch_file), _KEY)

    assert caplog.records == []
    assert not batch_file.exists()


async def test_upload_lands_in_the_store_when_the_component_exists(
    obs: _ConcreteObs, batch_file: Path, tmp_path: Path
) -> None:
    store_root = tmp_path / "store"
    write_component(
        tmp_path,
        DEPLOYMENT_OBJECT_STORE_NAME,
        "bindings.localstorage",
        {"rootPath": str(store_root)},
    )

    await obs._upload_and_delete(str(batch_file), _KEY)

    assert (store_root / _KEY).read_bytes() == b"telemetry"

"""Observability's object-store sink is installed by the bootstrap, not imported (FND-4260).

``application_sdk.observability`` does not import storage (storage logs through
observability, so that import was a cycle). The process bootstrap installs the
sink instead. These tests pin both halves: every bootstrap installs it, and an
export with no sink installed is dropped with one WARNING instead of silently.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from application_sdk.observability import observability
from application_sdk.storage._observability_sink import (
    install_observability_store_sink,
)


@pytest.fixture
def no_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_store_sink", None)
    monkeypatch.setattr(observability, "_missing_sink_warned", False)


def _never_called() -> object:
    raise AssertionError("no store is built without a sink")


@pytest.mark.usefixtures("no_sink")
class TestNoSinkInstalled:
    async def test_export_is_dropped_with_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING)
        for n in range(3):
            local = tmp_path / f"batch-{n}.json.gz"
            local.write_bytes(b"x")
            await observability._upload_to_observability_stores(
                str(local),
                f"artifacts/apps/observability/logs/{n}.json.gz",
                get_deployment_store=_never_called,
                get_upstream_store=_never_called,
            )
            # The local file is deleted either way: no disk leak per flush.
            assert not local.exists()
        warnings = [
            r for r in caplog.records if "No object-store sink installed" in r.message
        ]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING

    def test_store_getter_raises_not_configured(self) -> None:
        from application_sdk._context_errors import ObjectStoreNotConfiguredError

        observability.AtlanObservability._deployment_store = None
        with pytest.raises(ObjectStoreNotConfiguredError):
            observability.AtlanObservability._get_deployment_store()

    def test_no_binding_error_is_transient(self) -> None:
        from application_sdk.storage.errors import StorageBindingNotFoundError

        assert not observability._is_transient_binding_error(
            StorageBindingNotFoundError("deployment-objectstore")
        )


class TestSinkInstalled:
    async def test_export_uploads_through_storage_without_a_sidecar(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "batch.json.gz"
        local.write_bytes(b"x")
        store = object()
        with mock.patch(
            "application_sdk.storage.upload_file", new=mock.AsyncMock()
        ) as upload:
            await observability._upload_to_observability_stores(
                str(local),
                "artifacts/apps/observability/logs/k.json.gz",
                get_deployment_store=lambda: store,  # type: ignore[arg-type, return-value]
                get_upstream_store=_never_called,  # type: ignore[arg-type]
            )
        upload.assert_awaited_once_with(
            "artifacts/apps/observability/logs/k.json.gz",
            str(local),
            store=store,
            write_sidecar=False,
        )

    def test_binding_boot_race_is_transient(self) -> None:
        from application_sdk.storage.errors import (
            StorageBindingBrokenError,
            StorageBindingNotFoundError,
            StorageConfigError,
        )

        assert observability._is_transient_binding_error(
            StorageBindingNotFoundError("deployment-objectstore")
        )
        assert observability._is_transient_binding_error(
            StorageBindingBrokenError("deployment-objectstore")
        )
        assert not observability._is_transient_binding_error(
            StorageConfigError("unsupported binding type")
        )


@pytest.mark.usefixtures("no_sink")
class TestBootstrapsInstallTheSink:
    def test_create_app_handler_service(self) -> None:
        from application_sdk.handler.service import create_app_handler_service
        from tests.unit.handler._service_support import _TestHandler

        create_app_handler_service(_TestHandler(), app_name="sink-test")
        assert observability._store_sink is not None

    def test_create_worker(self) -> None:
        from application_sdk.execution._temporal import worker

        with mock.patch.object(
            worker,
            "install_observability_store_sink",
            wraps=install_observability_store_sink,
        ) as install:
            # The sink goes in first, before the worker reads its client.
            with pytest.raises(Exception):
                worker.create_worker(None)  # type: ignore[arg-type]
        install.assert_called_once_with()
        assert observability._store_sink is not None

    def test_importing_main(self) -> None:
        # A fresh interpreter: this process imported main long ago.
        code = (
            "import application_sdk.main\n"
            "from application_sdk.observability import observability as o\n"
            "assert o._store_sink is not None, 'main installed no sink'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr[-2000:]

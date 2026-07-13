"""SDR-mode coverage for AtlanObservability upstream object-store upload.

In SDR mode (``ENABLE_ATLAN_UPLOAD=true``) the app runs on customer infra and
must push observability signals to the *upstream* (Atlan) object store in
addition to the customer's deployment store, under an
``artifacts/apps/observability/sdr/{logs,metrics,traces}`` prefix.

The existing suite (``test_logger_adaptor.py::TestFlushRecordsSinglePass``)
covers only the deployment-store / non-SDR path.  These tests cover the
SDR-active surface:

* the import-time-derived ``_OBS_MODE`` / prefix maps flip to ``sdr`` when the
  flag is set,
* ``_upload_and_delete`` fans out to the *upstream* store (``_get_upstream_store``)
  in addition to the deployment store, with the same remote key, and
* an upstream-store upload failure is swallowed by ``_flush_records`` and never
  crashes the flush.

Stores are always mocked — no real cloud calls.
"""

from __future__ import annotations

import gzip
import importlib.util
from datetime import datetime
from typing import Any
from unittest import mock

import pytest

from application_sdk.constants import LOG_FILE_NAME, METRICS_FILE_NAME, TRACES_FILE_NAME
from application_sdk.observability.observability import AtlanObservability


def _load_observability_with_flag(enable_atlan_upload: bool):
    """Load a fresh, throwaway copy of the observability module with
    ``ENABLE_ATLAN_UPLOAD`` patched at import time.

    ``_OBS_MODE`` and the two prefix maps are derived at *import* time from the
    flag, so a plain runtime ``mock.patch`` of the constant does not recompute
    them.  This re-executes the module source into a private namespace (never
    touching the real ``sys.modules`` entry, so global test isolation is
    preserved) with the constant patched, and returns the fresh module.
    """
    import application_sdk.observability.observability as obs

    spec = importlib.util.spec_from_file_location(
        "observability_fresh_import_for_test", obs.__file__
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch(
        "application_sdk.constants.ENABLE_ATLAN_UPLOAD", enable_atlan_upload
    ):
        spec.loader.exec_module(module)
    return module


class _ConcreteObs(AtlanObservability):
    """Minimal concrete subclass so the abstract base can be instantiated.

    The upload machinery under test (``_flush_records`` / ``_upload_and_delete``
    / ``_build_remote_key``) all lives on the base class; ``process_record`` and
    ``export_record`` are the only abstract methods and are irrelevant here.
    """

    def process_record(self, record: Any) -> dict[str, Any]:
        return record

    def export_record(self, record: Any) -> None:
        pass


@pytest.fixture()
def obs_instance(tmp_path):
    """A concrete AtlanObservability writing logs into an isolated tmp dir."""
    AtlanObservability._reset_for_testing()
    inst = _ConcreteObs(
        batch_size=100,
        flush_interval=60,
        retention_days=7,
        cleanup_enabled=False,
        data_dir=str(tmp_path),
        file_name=LOG_FILE_NAME,  # → signal type "logs"
    )
    yield inst
    AtlanObservability._reset_for_testing()


def _make_record(ts: float, message: str = "test") -> dict[str, Any]:
    return {
        "timestamp": ts,
        "level": "INFO",
        "logger_name": "test",
        "message": message,
        "file": "test.py",
        "line": 1,
        "function": "test_fn",
        "extra": {},
    }


class TestObsModeDerivation:
    """(a) ``_OBS_MODE`` and prefix maps flip on ``ENABLE_ATLAN_UPLOAD``."""

    def test_sdr_mode_when_atlan_upload_enabled(self):
        mod = _load_observability_with_flag(True)
        assert mod._OBS_MODE == "sdr"
        assert mod.LOCAL_OBS_SUBDIR_MAP == {
            "logs": "sdr/logs",
            "metrics": "sdr/metrics",
            "traces": "sdr/traces",
        }
        assert mod.OBSERVABILITY_S3_PREFIX_MAP == {
            "logs": "artifacts/apps/observability/sdr/logs",
            "metrics": "artifacts/apps/observability/sdr/metrics",
            "traces": "artifacts/apps/observability/sdr/traces",
        }

    def test_non_sdr_mode_when_atlan_upload_disabled(self):
        mod = _load_observability_with_flag(False)
        assert mod._OBS_MODE == "non-sdr"
        assert mod.LOCAL_OBS_SUBDIR_MAP["logs"] == "non-sdr/logs"
        assert (
            mod.OBSERVABILITY_S3_PREFIX_MAP["logs"]
            == "artifacts/apps/observability/non-sdr/logs"
        )

    @pytest.mark.parametrize(
        ("file_name", "signal"),
        [
            (LOG_FILE_NAME, "logs"),
            (METRICS_FILE_NAME, "metrics"),
            (TRACES_FILE_NAME, "traces"),
        ],
    )
    def test_sdr_remote_key_built_with_sdr_prefix(self, file_name, signal):
        """A fresh SDR-mode module builds ``sdr/<signal>/``-prefixed remote keys.

        Parametrized across all three signals — the buffered upload path
        (``AtlanObservability``) backs the logs, metrics AND traces adaptors, so
        the ``sdr/`` prefix must hold for every ``_get_signal_type()`` value, not
        only ``logs``.
        """
        mod = _load_observability_with_flag(True)

        class _Obs(mod.AtlanObservability):
            def process_record(self, record):
                return record

            def export_record(self, record):
                pass

        mod.AtlanObservability._reset_for_testing()
        try:
            inst = _Obs(
                batch_size=1,
                flush_interval=60,
                retention_days=7,
                cleanup_enabled=False,
                data_dir="/tmp/does-not-need-to-exist-obs-test",
                file_name=file_name,
            )
            key = inst._build_remote_key(
                datetime(2025, 6, 15, 14, 0, 0), "file.json.gz"
            )
            # Assert the object-store key STRUCTURE, not the runner's path sep:
            # _build_remote_key uses os.path.join, which yields "\" on a Windows
            # CI runner. The SDR runtime is Linux (keys are always "/"), so
            # normalize here rather than couple the test to the runner's OS.
            key = key.replace("\\", "/")
            assert key.startswith(f"artifacts/apps/observability/sdr/{signal}/")
            assert "year=2025" in key and "hour=14" in key
        finally:
            mod.AtlanObservability._reset_for_testing()


class TestUpstreamUploadFanOut:
    """(b) ``_upload_and_delete`` fans out to the upstream store in SDR mode."""

    @pytest.mark.asyncio
    async def test_targets_both_deployment_and_upstream_store(
        self, obs_instance, tmp_path
    ):
        local = tmp_path / "part.json.gz"
        local.write_bytes(b"payload")
        deployment_store = mock.MagicMock(name="deployment_store")
        upstream_store = mock.MagicMock(name="upstream_store")
        remote_key = "artifacts/apps/observability/sdr/logs/x.json.gz"

        calls: list[tuple[str, str, Any]] = []

        async def _fake_upload_file(key, local_path, store=None, **_kw):
            calls.append((key, local_path, store))

        with (
            mock.patch(
                "application_sdk.observability.observability.ENABLE_ATLAN_UPLOAD",
                True,
            ),
            mock.patch(
                "application_sdk.storage.upload_file", side_effect=_fake_upload_file
            ),
            mock.patch.object(
                obs_instance, "_get_deployment_store", return_value=deployment_store
            ),
            mock.patch.object(
                obs_instance, "_get_upstream_store", return_value=upstream_store
            ),
        ):
            await obs_instance._upload_and_delete(str(local), remote_key)

        # Two uploads: deployment + upstream, both with the identical remote key.
        assert len(calls) == 2, calls
        stores_used = [c[2] for c in calls]
        assert deployment_store in stores_used
        assert upstream_store in stores_used
        assert all(c[0] == remote_key for c in calls)
        # Local file is always deleted afterwards (no disk leak).
        assert not local.exists()

    @pytest.mark.asyncio
    async def test_no_upstream_upload_when_flag_disabled(self, obs_instance, tmp_path):
        """Sanity: with the flag off, only the deployment store is targeted."""
        local = tmp_path / "part.json.gz"
        local.write_bytes(b"payload")
        deployment_store = mock.MagicMock(name="deployment_store")
        calls: list[Any] = []

        async def _fake_upload_file(key, local_path, store=None, **_kw):
            calls.append(store)

        with (
            mock.patch(
                "application_sdk.observability.observability.ENABLE_ATLAN_UPLOAD",
                False,
            ),
            mock.patch(
                "application_sdk.storage.upload_file", side_effect=_fake_upload_file
            ),
            mock.patch.object(
                obs_instance, "_get_deployment_store", return_value=deployment_store
            ),
            mock.patch.object(obs_instance, "_get_upstream_store") as get_upstream,
        ):
            await obs_instance._upload_and_delete(str(local), "some/key.json.gz")

        assert calls == [deployment_store]
        get_upstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_records_reaches_upstream_store(self, obs_instance, tmp_path):
        """End-to-end via ``_flush_records``: a real batch reaches the upstream
        store when SDR mode is on."""
        obs_instance.data_dir = str(tmp_path)
        base_ts = datetime(2025, 6, 15, 10, 30, 0).timestamp()
        records = [_make_record(base_ts + i, f"msg-{i}") for i in range(3)]

        deployment_store = mock.MagicMock(name="deployment_store")
        upstream_store = mock.MagicMock(name="upstream_store")
        upstream_uploads: list[str] = []

        async def _fake_upload_file(key, local_path, store=None, **_kw):
            if store is upstream_store:
                upstream_uploads.append(key)

        with (
            mock.patch(
                "application_sdk.observability.observability.ENABLE_ATLAN_UPLOAD",
                True,
            ),
            mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK",
                True,
            ),
            mock.patch(
                "application_sdk.storage.upload_file", side_effect=_fake_upload_file
            ),
            mock.patch.object(
                obs_instance, "_get_deployment_store", return_value=deployment_store
            ),
            mock.patch.object(
                obs_instance, "_get_upstream_store", return_value=upstream_store
            ),
        ):
            await obs_instance._flush_records(records)

        assert len(upstream_uploads) == 1, upstream_uploads


class TestUpstreamUploadFailureSwallowed:
    """(c) An upstream-upload failure must not crash the flush."""

    @pytest.mark.asyncio
    async def test_flush_records_swallows_upstream_failure(
        self, obs_instance, tmp_path
    ):
        obs_instance.data_dir = str(tmp_path)
        base_ts = datetime(2025, 6, 15, 10, 30, 0).timestamp()
        records = [_make_record(base_ts, "msg")]

        deployment_store = mock.MagicMock(name="deployment_store")
        upstream_store = mock.MagicMock(name="upstream_store")
        seen_stores: list[Any] = []

        async def _fake_upload_file(key, local_path, store=None, **_kw):
            seen_stores.append(store)
            if store is upstream_store:
                raise OSError("simulated upstream upload failure")

        with (
            mock.patch(
                "application_sdk.observability.observability.ENABLE_ATLAN_UPLOAD",
                True,
            ),
            mock.patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK",
                True,
            ),
            mock.patch(
                "application_sdk.storage.upload_file", side_effect=_fake_upload_file
            ),
            mock.patch.object(
                obs_instance, "_get_deployment_store", return_value=deployment_store
            ),
            mock.patch.object(
                obs_instance, "_get_upstream_store", return_value=upstream_store
            ),
        ):
            # Must return normally despite the upstream store raising.
            await obs_instance._flush_records(records)

        # Deployment upload was reached first, then the upstream attempt raised.
        assert deployment_store in seen_stores
        assert upstream_store in seen_stores
        # No orphaned local partition files left behind after the swallowed error.
        leftovers = list(tmp_path.rglob("*.json.gz"))
        assert leftovers == [], leftovers

    @pytest.mark.asyncio
    async def test_upload_and_delete_deletes_local_file_even_on_upstream_failure(
        self, obs_instance, tmp_path
    ):
        """The local file is unlinked in the ``finally`` even when the upstream
        upload raises (the exception propagates out of ``_upload_and_delete``)."""
        local = tmp_path / "part.json.gz"
        with gzip.open(local, "wb") as fh:
            fh.write(b'{"x": 1}\n')
        upstream_store = mock.MagicMock(name="upstream_store")

        async def _fake_upload_file(key, local_path, store=None, **_kw):
            if store is upstream_store:
                raise RuntimeError("upstream boom")

        with (
            mock.patch(
                "application_sdk.observability.observability.ENABLE_ATLAN_UPLOAD",
                True,
            ),
            mock.patch(
                "application_sdk.storage.upload_file", side_effect=_fake_upload_file
            ),
            mock.patch.object(
                obs_instance,
                "_get_deployment_store",
                return_value=mock.MagicMock(name="deployment_store"),
            ),
            mock.patch.object(
                obs_instance, "_get_upstream_store", return_value=upstream_store
            ),
        ):
            with pytest.raises(RuntimeError, match="upstream boom"):
                await obs_instance._upload_and_delete(str(local), "sdr/logs/x.json.gz")

        # Even though the upstream upload raised, the local file is cleaned up.
        assert not local.exists()

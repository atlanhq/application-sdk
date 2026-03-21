"""Tests for App.cleanup_storage() framework task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, TaskStateAccessor, _app_state, _app_state_lock
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import StorageCleanupInput, StorageCleanupOutput
from application_sdk.contracts.types import FileReference, StorageTier


@dataclass
class _CSInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _CSOutput(Output, allow_unbounded_fields=True):
    result: str = ""


def _make_app() -> App:
    """Create a minimal App instance for testing cleanup_storage."""

    class _CleanupApp(App):
        async def run(self, input: _CSInput) -> _CSOutput:
            return _CSOutput()

    return _CleanupApp()


def _seed_tracked_refs(refs: list[FileReference], workflow_id: str = "wf-test") -> None:
    """Seed _app_state with tracked FileReference objects under a fake workflow_id."""
    with mock.patch(
        "application_sdk.app.base._get_execution_id_from_task",
        return_value=workflow_id,
    ):
        accessor = TaskStateAccessor()
        from application_sdk.constants import TRACKED_FILE_REFS_KEY

        accessor.set(TRACKED_FILE_REFS_KEY, refs)


def _clear_app_state(workflow_id: str = "wf-test") -> None:
    with _app_state_lock:
        _app_state.pop(workflow_id, None)


class TestCleanupStorage:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        _clear_app_state()

    # ------------------------------------------------------------------
    # No store configured → immediate zero-count return
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_store_returns_zero_counts(self) -> None:
        app = _make_app()
        # _context is None → storage is None
        result = await app.cleanup_storage(StorageCleanupInput())
        assert result == StorageCleanupOutput(
            deleted_count=0, skipped_count=0, error_count=0
        )

    @pytest.mark.asyncio
    async def test_no_store_with_context_but_no_storage(self) -> None:
        from application_sdk.app.context import AppContext

        app = _make_app()
        app._context = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        # context.storage is None by default
        result = await app.cleanup_storage(StorageCleanupInput())
        assert result == StorageCleanupOutput(
            deleted_count=0, skipped_count=0, error_count=0
        )

    # ------------------------------------------------------------------
    # Tracked file_refs/ paths are deleted (+ .sha256 sidecars)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_deletes_tracked_file_refs_and_sidecars(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        ref = FileReference(storage_path="file_refs/abc123.parquet")
        _seed_tracked_refs([ref])

        deleted_keys: list[str] = []

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            deleted_keys.append(key)
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    with mock.patch("obstore.list", return_value=iter([])):
                        result = await app.cleanup_storage(StorageCleanupInput())

        assert "file_refs/abc123.parquet" in deleted_keys
        assert "file_refs/abc123.parquet.sha256" in deleted_keys
        assert result.deleted_count == 2
        assert result.error_count == 0

    # ------------------------------------------------------------------
    # Protected prefixes are skipped
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_skips_protected_persistent_artifacts(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        ref = FileReference(
            storage_path="persistent-artifacts/apps/test/connection/123/config.json"
        )
        _seed_tracked_refs([ref])

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    result = await app.cleanup_storage(StorageCleanupInput())

        assert result.skipped_count == 1
        assert result.deleted_count == 0

    # ------------------------------------------------------------------
    # RETAINED and PERSISTENT tier refs are skipped by cleanup_storage
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_skips_retained_tier_ref(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        ref = FileReference(
            storage_path="artifacts/apps/test/workflows/wf1/run1/file_refs/abc.parquet",
            tier=StorageTier.RETAINED,
        )
        _seed_tracked_refs([ref])

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    result = await app.cleanup_storage(StorageCleanupInput())

        assert result.skipped_count == 1
        assert result.deleted_count == 0

    @pytest.mark.asyncio
    async def test_skips_persistent_tier_ref(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        ref = FileReference(
            storage_path="persistent-artifacts/apps/test/file_refs/abc.parquet",
            tier=StorageTier.PERSISTENT,
        )
        _seed_tracked_refs([ref])

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    result = await app.cleanup_storage(StorageCleanupInput())

        assert result.skipped_count == 1
        assert result.deleted_count == 0

    # ------------------------------------------------------------------
    # include_prefix_cleanup=True triggers run-scoped prefix deletion
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_prefix_cleanup_when_opted_in(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        batch_items = [
            {"path": "artifacts/apps/test/workflows/wf1/run1/output.parquet"}
        ]
        deleted_keys: list[str] = []

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            deleted_keys.append(key)
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    with mock.patch(
                        "application_sdk.execution._temporal.activity_utils.build_output_path",
                        return_value="artifacts/apps/test/workflows/wf1/run1",
                    ):
                        with mock.patch(
                            "obstore.list", return_value=iter([batch_items])
                        ):
                            result = await app.cleanup_storage(
                                StorageCleanupInput(include_prefix_cleanup=True)
                            )

        assert "artifacts/apps/test/workflows/wf1/run1/output.parquet" in deleted_keys
        assert result.deleted_count >= 1

    # ------------------------------------------------------------------
    # include_prefix_cleanup=False (default) does NOT call prefix deletion
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_prefix_cleanup_by_default(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        list_calls: list[str] = []

        def _tracking_list(resolved: Any, prefix: str = "") -> Any:
            list_calls.append(prefix)
            return iter([])

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch("obstore.list", side_effect=_tracking_list):
                    result = await app.cleanup_storage(StorageCleanupInput())

        # No list calls for run-scoped prefix when include_prefix_cleanup=False
        assert not any(
            "artifacts/apps/" in c for c in list_calls
        ), f"Unexpected prefix list calls: {list_calls}"
        assert result == StorageCleanupOutput()

    # ------------------------------------------------------------------
    # Individual delete errors increment error_count, others continue
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_error_increments_error_count(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext
        from application_sdk.storage.errors import StorageError

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        refs = [
            FileReference(storage_path="file_refs/aaa.parquet"),
            FileReference(storage_path="file_refs/bbb.parquet"),
        ]
        _seed_tracked_refs(refs)

        call_count = 0

        async def _flaky_delete(key: str, store: Any, normalize: bool = True) -> bool:
            nonlocal call_count
            call_count += 1
            if "aaa" in key:
                raise StorageError("network error", key=key)
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_flaky_delete
                ):
                    with mock.patch("obstore.list", return_value=iter([])):
                        result = await app.cleanup_storage(StorageCleanupInput())

        # aaa → 2 errors (key + .sha256), bbb → 2 successes
        assert result.error_count == 2
        assert result.deleted_count == 2

    # ------------------------------------------------------------------
    # Directory FileReference (storage_path ending with "/") streams sub-keys
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_directory_ref_streams_subkeys(self) -> None:
        from unittest.mock import MagicMock

        from application_sdk.app.context import AppContext

        store = MagicMock()
        app = _make_app()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx

        ref = FileReference(storage_path="file_refs/dir123/")
        _seed_tracked_refs([ref])

        batch_items = [
            {"path": "file_refs/dir123/part-0.parquet"},
            {"path": "file_refs/dir123/part-1.parquet"},
        ]
        deleted_keys: list[str] = []

        async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
            deleted_keys.append(key)
            return True

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            with mock.patch(
                "application_sdk.storage.ops._resolve_store", return_value=store
            ):
                with mock.patch(
                    "application_sdk.storage.ops.delete", side_effect=_mock_delete
                ):
                    with mock.patch("obstore.list", return_value=iter([batch_items])):
                        result = await app.cleanup_storage(StorageCleanupInput())

        assert "file_refs/dir123/part-0.parquet" in deleted_keys
        assert "file_refs/dir123/part-1.parquet" in deleted_keys
        assert result.deleted_count == 2

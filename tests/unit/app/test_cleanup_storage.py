"""Tests for App.cleanup_storage() framework task."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, TaskStateAccessor, _app_state, _app_state_lock
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import StorageCleanupInput, StorageCleanupOutput
from application_sdk.contracts.storage import DownloadInput
from application_sdk.contracts.types import FileReference, StorageTier, StoreTarget
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _put


@dataclass
class _CSInput(Input, allow_unbounded_fields=True):
    value: str = ""


@dataclass
class _CSOutput(Output, allow_unbounded_fields=True):
    result: str = ""


async def _stream(batches: list[Any]) -> AsyncIterator[Any]:
    """Stand-in for obstore's ListStream, which cleanup_storage reads with ``async for``."""
    for batch in batches:
        yield batch


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
                    with mock.patch(
                        "obstore.list", side_effect=lambda *a, **k: _stream([])
                    ):
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
                        "application_sdk.execution.build_output_path",
                        return_value="artifacts/apps/test/workflows/wf1/run1",
                    ):
                        with mock.patch(
                            "obstore.list",
                            side_effect=lambda *a, **k: _stream([batch_items]),
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
            return _stream([])

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
                    with mock.patch(
                        "obstore.list", side_effect=lambda *a, **k: _stream([])
                    ):
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
                    with mock.patch(
                        "obstore.list",
                        side_effect=lambda *a, **k: _stream([batch_items]),
                    ):
                        result = await app.cleanup_storage(StorageCleanupInput())

        assert "file_refs/dir123/part-0.parquet" in deleted_keys
        assert "file_refs/dir123/part-1.parquet" in deleted_keys
        assert result.deleted_count == 2


async def _run_cleanup_capturing_deletes(
    refs: list[FileReference],
    listing: list[list[dict[str, str]]] | None = None,
    input: StorageCleanupInput | None = None,
) -> tuple[StorageCleanupOutput, list[str]]:
    """Run ``cleanup_storage`` over *refs*; return its output and every key deleted."""
    from unittest.mock import MagicMock

    from application_sdk.app.context import AppContext

    store = MagicMock()
    app = _make_app()
    ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
    ctx._storage = store
    app._context = ctx
    _seed_tracked_refs(refs)

    deleted_keys: list[str] = []

    async def _mock_delete(key: str, store: Any, normalize: bool = True) -> bool:
        deleted_keys.append(key)
        return True

    with (
        mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ),
        mock.patch("application_sdk.storage.ops._resolve_store", return_value=store),
        mock.patch("application_sdk.storage.ops.delete", side_effect=_mock_delete),
        mock.patch("obstore.list", side_effect=lambda *a, **k: _stream(listing or [])),
        mock.patch(
            "application_sdk.execution.build_output_path",
            return_value="artifacts/apps/test/workflows/wf-test/r1",
        ),
    ):
        result = await app.cleanup_storage(input or StorageCleanupInput())
    return result, deleted_keys


class TestCleanupStorageNeverDeletesDurableAlias:
    """A TRANSIENT ref aliasing a key that a RETAINED/PERSISTENT ref in the same
    run tracks must not delete that key.

    Regression for a connector that hand-built TRANSIENT refs on the keys
    ``upload_refs`` had delivered as RETAINED: cleanup skipped the RETAINED refs,
    then deleted the same keys through their TRANSIENT twins.
    """

    _KEY = "artifacts/apps/test/workflows/wf-test/r1/transformed/table/entities.json"

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        _clear_app_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", [StorageTier.RETAINED, StorageTier.PERSISTENT])
    async def test_transient_twin_of_durable_file_is_not_deleted(
        self, tier: StorageTier
    ) -> None:
        refs = [
            FileReference(storage_path=self._KEY, tier=tier),
            FileReference(storage_path=self._KEY, tier=StorageTier.TRANSIENT),
        ]
        result, deleted = await _run_cleanup_capturing_deletes(refs)

        assert self._KEY not in deleted
        assert self._KEY + ".sha256" not in deleted
        assert result.deleted_count == 0
        # One skip for the durable ref, two for the twin's key and its sidecar.
        assert result.skipped_count == 3

    @pytest.mark.asyncio
    async def test_unrelated_transient_ref_is_still_deleted(self) -> None:
        other = "file_refs/abc123.parquet"
        refs = [
            FileReference(storage_path=self._KEY, tier=StorageTier.RETAINED),
            FileReference(storage_path=self._KEY, tier=StorageTier.TRANSIENT),
            FileReference(storage_path=other, tier=StorageTier.TRANSIENT),
        ]
        _, deleted = await _run_cleanup_capturing_deletes(refs)

        assert sorted(deleted) == [other, other + ".sha256"]

    @pytest.mark.asyncio
    async def test_transient_file_under_durable_directory_is_not_deleted(
        self,
    ) -> None:
        durable_dir = "artifacts/apps/test/workflows/wf-test/r1/transformed/"
        refs = [
            FileReference(storage_path=durable_dir, tier=StorageTier.RETAINED),
            FileReference(storage_path=self._KEY, tier=StorageTier.TRANSIENT),
        ]
        _, deleted = await _run_cleanup_capturing_deletes(refs)

        assert deleted == []

    @pytest.mark.asyncio
    async def test_transient_directory_skips_only_durable_subkeys(self) -> None:
        transient_dir = "artifacts/apps/test/workflows/wf-test/r1/transformed/"
        other = transient_dir + "column/entities.json"
        refs = [
            FileReference(storage_path=self._KEY, tier=StorageTier.RETAINED),
            FileReference(storage_path=transient_dir, tier=StorageTier.TRANSIENT),
        ]
        listing = [
            [{"path": self._KEY}, {"path": self._KEY + ".sha256"}, {"path": other}]
        ]
        result, deleted = await _run_cleanup_capturing_deletes(refs, listing)

        assert deleted == [other]
        assert result.deleted_count == 1

    @pytest.mark.asyncio
    async def test_prefix_cleanup_still_removes_retained_keys(self) -> None:
        # Opt-in prefix cleanup is the documented way to drop RETAINED refs;
        # the alias guard must not turn it into a no-op.
        refs = [FileReference(storage_path=self._KEY, tier=StorageTier.RETAINED)]
        listing = [[{"path": self._KEY}]]
        _, deleted = await _run_cleanup_capturing_deletes(
            refs, listing, StorageCleanupInput(include_prefix_cleanup=True)
        )

        assert deleted == [self._KEY]


class TestCleanupStorageDirectoryManifest:
    """FND-3762: a directory ref's manifest sits outside its prefix by design,
    so the prefix listing never reaches it. Cleanup must delete it with a
    TRANSIENT ref and protect it for a durable one."""

    _DIR = "artifacts/apps/test/workflows/wf-test/r1/file_refs/d1/"
    _MANIFEST = "artifacts/apps/test/workflows/wf-test/r1/file_refs/m1.sdk-manifest"

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        _clear_app_state()

    @pytest.mark.asyncio
    async def test_transient_directory_ref_deletes_its_manifest(self) -> None:
        ref = FileReference(storage_path=self._DIR, manifest_path=self._MANIFEST)
        listing = [[{"path": self._DIR + "a.json"}]]

        result, deleted = await _run_cleanup_capturing_deletes([ref], listing)

        assert sorted(deleted) == sorted([self._DIR + "a.json", self._MANIFEST])
        assert result.deleted_count == 2

    @pytest.mark.asyncio
    async def test_manifest_under_a_protected_prefix_is_never_deleted(self) -> None:
        protected = "persistent-artifacts/apps/test/file_refs/m2.sdk-manifest"
        ref = FileReference(storage_path=self._DIR, manifest_path=protected)

        result, deleted = await _run_cleanup_capturing_deletes([ref], [])

        assert protected not in deleted
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_transient_twin_does_not_delete_a_retained_manifest(self) -> None:
        refs = [
            FileReference(
                storage_path=self._DIR,
                manifest_path=self._MANIFEST,
                tier=StorageTier.RETAINED,
            ),
            FileReference(storage_path=self._DIR, manifest_path=self._MANIFEST),
        ]

        _, deleted = await _run_cleanup_capturing_deletes(refs, [])

        assert self._MANIFEST not in deleted


class TestCleanupStorageSparesDownloadedKeys:
    """FND-3815: ``App.download`` reads keys the app does not own.

    The interceptor tracks the ref ``download`` returns. It used to be
    TRANSIENT, so end-of-run cleanup deleted the downloaded key from the
    deployment store — in a single-store deployment that is the shared
    bucket, so a platform object (the connection cache) was gone for every
    later run. Real in-memory stores, the real tracker, the real delete.
    """

    _KEY = "artifacts/apps/producer/workflows/wf-0/run-0/output/part-0.json"
    _DATA = b'{"a": 1}\n'

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        _clear_app_state()

    def _app(self, deployment: Any, upstream: Any | None) -> App:
        from application_sdk.app.context import AppContext

        app = _make_app()
        app._context = AppContext(
            app_name="test",
            app_version="0.1.0",
            run_id="r1",
            _storage=deployment,
            _upstream_storage=upstream,
        )
        return app

    async def _download_then_cleanup(
        self, app: App, input: DownloadInput
    ) -> StorageCleanupOutput:
        from application_sdk.execution._temporal.activities import _track_file_refs

        dl = await app.download(input)
        # What the activity interceptor does with a task's output refs.
        _track_file_refs("wf-test", dl.ref)
        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-test",
        ):
            return await app.cleanup_storage(StorageCleanupInput())

    @staticmethod
    async def _exists(store: Any, key: str) -> bool:
        from application_sdk.storage.ops import exists

        return await exists(key, store, normalize=False)

    @pytest.mark.asyncio
    async def test_single_store_download_survives_cleanup(self) -> None:
        store = create_memory_store()
        await _put(self._KEY, self._DATA, store)

        result = await self._download_then_cleanup(
            self._app(store, None), DownloadInput(storage_path=self._KEY)
        )

        assert await self._exists(store, self._KEY)
        assert result.deleted_count == 0

    @pytest.mark.asyncio
    async def test_single_store_explicit_transient_download_is_cleaned(
        self,
    ) -> None:
        """The opt-in still works, so the test above is not a dead cleanup."""
        store = create_memory_store()
        await _put(self._KEY, self._DATA, store)

        await self._download_then_cleanup(
            self._app(store, None),
            DownloadInput(storage_path=self._KEY, tier=StorageTier.TRANSIENT),
        )

        assert not await self._exists(store, self._KEY)

    @pytest.mark.asyncio
    async def test_redownload_of_a_runs_transient_ref_is_still_cleaned(
        self,
    ) -> None:
        """Re-materialising the run's own TRANSIENT ref keeps its tier.

        A RETAINED download ref would alias the producer's TRANSIENT ref, and
        the durable-alias rule would then leak the run's own output.
        """
        from application_sdk.execution._temporal.activities import _track_file_refs

        store = create_memory_store()
        key = "file_refs/abc123.parquet"
        await _put(key, self._DATA, store)
        produced = FileReference(storage_path=key, is_durable=True)
        _track_file_refs("wf-test", produced)

        await self._download_then_cleanup(
            self._app(store, None), DownloadInput(ref=produced)
        )

        assert not await self._exists(store, key)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("input_tier", "ref_tier", "expected"),
        [
            (None, None, StorageTier.RETAINED),
            (None, StorageTier.TRANSIENT, StorageTier.TRANSIENT),
            (None, StorageTier.PERSISTENT, StorageTier.PERSISTENT),
            (StorageTier.TRANSIENT, StorageTier.PERSISTENT, StorageTier.TRANSIENT),
        ],
    )
    async def test_download_ref_tier(
        self,
        input_tier: StorageTier | None,
        ref_tier: StorageTier | None,
        expected: StorageTier,
    ) -> None:
        store = create_memory_store()
        await _put(self._KEY, self._DATA, store)
        ref = (
            None
            if ref_tier is None
            else FileReference(storage_path=self._KEY, is_durable=True, tier=ref_tier)
        )

        dl = await self._app(store, None).download(
            DownloadInput(
                storage_path="" if ref else self._KEY, ref=ref, tier=input_tier
            )
        )

        assert dl.ref.tier is expected

    @pytest.mark.asyncio
    async def test_explicit_path_other_than_the_ref_is_retained(self) -> None:
        store = create_memory_store()
        await _put(self._KEY, self._DATA, store)
        ref = FileReference(storage_path="file_refs/other.parquet", is_durable=True)

        dl = await self._app(store, None).download(
            DownloadInput(storage_path=self._KEY, ref=ref)
        )

        assert dl.ref.tier is StorageTier.RETAINED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", [StorageTier.RETAINED, StorageTier.TRANSIENT])
    async def test_two_store_download_never_deletes_from_deployment(
        self, tier: StorageTier
    ) -> None:
        """Even a TRANSIENT upstream ref: its bytes were never in deployment."""
        deployment, upstream = create_memory_store(), create_memory_store()
        await _put(self._KEY, self._DATA, upstream)
        await _put(self._KEY, b"deployment's own object\n", deployment)

        result = await self._download_then_cleanup(
            self._app(deployment, upstream),
            DownloadInput(storage_path=self._KEY, tier=tier),
        )

        assert await self._exists(deployment, self._KEY)
        assert await self._exists(upstream, self._KEY)
        assert result.deleted_count == 0

    @pytest.mark.asyncio
    async def test_prefix_sweep_still_removes_runs_deployment_copy(self) -> None:
        """Opt-in sweep owns the run prefix, whatever store the ref names.

        ``App.upload`` dual-writes the same run-prefix key to both stores and
        returns an UPSTREAM ref. Skipping that key in the sweep would leak the
        run's own deployment copy; the upstream copy is never touched.
        """
        from application_sdk.execution._temporal.activities import _track_file_refs

        run_prefix = "artifacts/apps/test/workflows/wf-test/r1"
        key = f"{run_prefix}/transformed/0.json"
        deployment, upstream = create_memory_store(), create_memory_store()
        await _put(key, self._DATA, deployment)
        await _put(key, self._DATA, upstream)
        _track_file_refs(
            "wf-test",
            FileReference(
                storage_path=key,
                is_durable=True,
                tier=StorageTier.RETAINED,
                store=StoreTarget.UPSTREAM,
            ),
        )

        with (
            mock.patch(
                "application_sdk.app.base._get_execution_id_from_task",
                return_value="wf-test",
            ),
            mock.patch(
                "application_sdk.execution.build_output_path", return_value=run_prefix
            ),
        ):
            await self._app(deployment, upstream).cleanup_storage(
                StorageCleanupInput(include_prefix_cleanup=True)
            )

        assert not await self._exists(deployment, key)
        assert await self._exists(upstream, key)

    @pytest.mark.asyncio
    async def test_transient_ref_under_connection_cache_is_not_deleted(
        self,
    ) -> None:
        store = create_memory_store()
        key = "connection-cache/example-connection.sqlite"
        await _put(key, b"sqlite", store)

        await self._download_then_cleanup(
            self._app(store, None),
            DownloadInput(storage_path=key, tier=StorageTier.TRANSIENT),
        )

        assert await self._exists(store, key)

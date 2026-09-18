"""BLDX-1619: App.upload must not silently fall back when upstream is required.

``ENABLE_ATLAN_UPLOAD=true`` means the deployment expects artifacts to land in
Atlan's bucket.  When the upstream store fails to resolve, the old code wrote
to the deployment store and returned a healthy positive file count, so the run
looked successful while publish saw nothing.  ``raise_on_empty`` cannot catch
this — it counts local source files, not what reached the destination.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output


class _In(Input, allow_unbounded_fields=True):
    pass


class _Out(Output, allow_unbounded_fields=True):
    pass


class _UploadApp(App):
    async def run(self, input: _In) -> _Out:
        return _Out()


class TestUploadRequiresUpstreamWhenAtlanUploadEnabled:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _app(self, *, upstream: object | None, deployment: object | None) -> App:
        from application_sdk.app.context import AppContext

        app = _UploadApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=deployment,  # type: ignore[arg-type]
            _upstream_storage=upstream,  # type: ignore[arg-type]
        )
        return app

    async def test_raises_when_atlan_upload_is_enabled_but_upstream_is_missing(
        self,
    ) -> None:
        from application_sdk.app.base_errors import (
            UpstreamObjectStoreNotConfiguredError,
        )
        from application_sdk.contracts.storage import UploadInput

        app = self._app(upstream=None, deployment=object())

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            mock.patch(
                "application_sdk.storage.transfer.upload", new_callable=mock.AsyncMock
            ) as mock_upload,
            pytest.raises(UpstreamObjectStoreNotConfiguredError),
        ):
            await app.upload(UploadInput(local_path="/tmp/out"))

        mock_upload.assert_not_awaited()

    async def test_falls_back_to_deployment_when_atlan_upload_is_disabled(self) -> None:
        """Non-SDR deployments keep the existing tolerant routing."""
        from application_sdk.contracts.storage import UploadInput, UploadOutput

        deployment = object()
        app = self._app(upstream=None, deployment=deployment)

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", False),
            mock.patch(
                "application_sdk.storage.transfer.upload",
                new_callable=mock.AsyncMock,
                return_value=UploadOutput(),
            ) as mock_upload,
        ):
            await app.upload(UploadInput(local_path="/tmp/out"))

        assert mock_upload.call_args.kwargs["store"] is deployment

    async def test_upstream_present_uploads_normally_with_atlan_upload_enabled(
        self,
    ) -> None:
        from application_sdk.contracts.storage import UploadInput, UploadOutput

        upstream = object()
        app = self._app(upstream=upstream, deployment=None)

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            mock.patch(
                "application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED",
                False,
            ),
            mock.patch(
                "application_sdk.storage.transfer.upload",
                new_callable=mock.AsyncMock,
                return_value=UploadOutput(),
            ) as mock_upload,
        ):
            await app.upload(UploadInput(local_path="/tmp/out"))

        assert mock_upload.call_args.kwargs["store"] is upstream

    async def test_aliased_store_is_one_write_not_a_self_copy(self) -> None:
        """Both names on one component: one handle, aliased — so one write.

        In-cluster charts wire UPSTREAM_OBJECT_STORE_NAME and
        DEPLOYMENT_OBJECT_STORE_NAME to the same component, and startup aliases
        the upstream handle to the deployment store.  The dual-write fan-out
        tests ``upstream is not deployment``, so it must collapse to a single
        write against that one bucket rather than copying it onto itself — and
        the ENABLE_ATLAN_UPLOAD guard must not fire, because a store is there.
        """
        from application_sdk.contracts.storage import UploadInput, UploadOutput

        deployment = object()
        app = self._app(upstream=deployment, deployment=deployment)
        assert app.context.single_store is True

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            mock.patch(
                "application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED",
                True,
            ),
            mock.patch(
                "application_sdk.storage.transfer.upload",
                new_callable=mock.AsyncMock,
                return_value=UploadOutput(),
            ) as mock_upload,
        ):
            await app.upload(UploadInput(local_path="/tmp/out"))

        mock_upload.assert_awaited_once()
        assert mock_upload.call_args.kwargs["store"] is deployment
        # The same object goes in as both target and fallback source, which is
        # the precondition transfer's identity guards test — see
        # TestAliasedStoreMovesNoBytes for the other half of that chain.
        assert mock_upload.call_args.kwargs["_source_store"] is deployment

    async def test_error_names_the_upstream_component_so_it_is_actionable(self) -> None:
        from application_sdk.app.base_errors import (
            UpstreamObjectStoreNotConfiguredError,
        )
        from application_sdk.contracts.storage import UploadInput

        app = self._app(upstream=None, deployment=object())

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            mock.patch(
                "application_sdk.storage.transfer.upload", new_callable=mock.AsyncMock
            ),
            pytest.raises(UpstreamObjectStoreNotConfiguredError) as exc,
        ):
            await app.upload(UploadInput(local_path="/tmp/out"))

        assert "ENABLE_ATLAN_UPLOAD" in str(exc.value)
        assert "UPSTREAM_OBJECT_STORE_NAME" in str(exc.value)
        assert exc.value.effective_retryable is False


class TestAliasedStoreMovesNoBytes:
    """CONNECT-1778: one bucket under two names must never be copied onto itself.

    ``App.upload`` hands every leg ``_source_store=self.context.storage`` and
    ``store=<target>``.  On the in-cluster wiring startup aliases the two
    handles, so both arguments are the *same object* and ``transfer.upload``'s
    identity gate (``source_resolved is not resolved``) skips the cross-store
    reconcile — the LIST plus per-key SHA-256 sidecar compare whose cost, on a
    multi-thousand-file prefix, exhausted the framework task's fixed
    ``600 s x 3`` budget and failed a run whose extraction had completed.

    This exercises the real ``transfer.upload`` against a real in-memory store,
    so it proves the whole path rather than the App layer's call shape.
    ``tests/unit/storage/test_transfer.py::…::test_same_store_identity_skips_reconcile``
    pins the same gate from the transfer side.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    async def test_aliased_upload_never_lists_the_source_store(self, tmp_path) -> None:
        from application_sdk.app.context import AppContext
        from application_sdk.contracts.storage import UploadInput
        from application_sdk.contracts.types import FileReference
        from application_sdk.storage.batch import list_data_keys
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _put

        store = create_memory_store()

        # An object that exists only in the store, under the ref's prefix. A
        # cross-store reconcile would stream it to the destination prefix; the
        # identity gate must leave it exactly where it is.
        src_prefix = "pfx/transformed"
        await _put(f"{src_prefix}/table/entities.json", b"T", store, normalize=False)

        local = tmp_path / "transformed"
        (local / "column").mkdir(parents=True)
        (local / "column" / "entities.json").write_bytes(b"C")

        app = _UploadApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            workflow_id="wf-1",
            run_id="run-1",
            _storage=store,  # type: ignore[arg-type]
            _upstream_storage=store,  # aliased: one component, two names  # type: ignore[arg-type]
        )
        assert app.context.single_store is True

        with (
            mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True),
            mock.patch(
                "application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED",
                True,
            ),
            mock.patch(
                "application_sdk.app.base._warn_on_invalid_transformed_assets",
                new_callable=mock.AsyncMock,
            ),
            # The reconcile branch's real call site. Patched as a spy so a
            # regression shows up as "it listed the source" rather than as a
            # slow test.
            mock.patch("application_sdk.storage.transfer.list_data_keys") as list_spy,
        ):
            await app.upload(
                UploadInput(
                    local_path=str(local),
                    ref=FileReference(local_path=str(local), storage_path=src_prefix),
                )
            )

        # THE assertion: no source-store LIST, so no per-key sidecar compare.
        list_spy.assert_not_called()

        keys = set(await list_data_keys("", store, normalize=False))
        # The local file was uploaded once, under the run prefix.
        landed = [k for k in keys if k.endswith("column/entities.json")]
        assert len(landed) == 1, keys
        # The store-only object was not copied anywhere: it exists at its
        # original key and nowhere else.
        assert [k for k in keys if k.endswith("table/entities.json")] == [
            f"{src_prefix}/table/entities.json"
        ], keys

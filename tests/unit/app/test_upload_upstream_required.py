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

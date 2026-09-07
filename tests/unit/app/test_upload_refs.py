"""FND-1790: App.upload_refs delivers a declaration, never a directory scan.

The fan-in half of the handoff.  Connectors that cross the SDR store boundary
have been hand-rolling this loop, and the hand-rolled version that scans
``os.path.join(output_path, "transformed")`` is wrong on any fanned-out run:
that directory is written by the activities, so the calling pod sees only the
subset that ran locally — and on a fully distributed run, nothing at all.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import (
    DeclaredFile,
    UploadOutput,
    UploadRefsInput,
)
from application_sdk.contracts.types import FileReference, StorageTier, StoreTarget
from application_sdk.storage.errors import UnplaceableDeclaredFileError

SOURCE = "artifacts/apps/a/workflows/wf-1/run-1/transformed"
DEST = "artifacts/apps/atlan/workflows/wf-1/run-1/transformed"


class _In(Input, allow_unbounded_fields=True):
    pass


class _Out(Output, allow_unbounded_fields=True):
    pass


class _UploadRefsApp(App):
    async def run(self, input: _In) -> _Out:
        return _Out()


def _ref(entity: str, *, prefix: str = SOURCE) -> FileReference:
    return FileReference(
        local_path=f"/tmp/transformed/{entity}/entities.json",
        storage_path=f"{prefix}/{entity}/entities.json",
        is_durable=True,
    )


class TestUploadRefs:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _app(self) -> App:
        from application_sdk.app.context import AppContext

        app = _UploadRefsApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=object(),
            _upstream_storage=object(),
        )
        return app

    def _patch_upload(self, app: App) -> mock.AsyncMock:
        """Stand in for ``_upload_impl``, echoing the destination key back."""

        async def _impl(input):  # noqa: ANN001 — mirrors UploadInput at the seam
            return UploadOutput(
                ref=FileReference(
                    local_path=input.local_path or None,
                    storage_path=input.storage_path,
                    is_durable=True,
                ),
                synced=True,
                reason="uploaded",
            )

        patcher = mock.patch.object(
            app, "_upload_impl", side_effect=_impl, new_callable=mock.AsyncMock
        )
        return patcher

    async def test_each_ref_is_uploaded_by_reference_under_the_new_prefix(
        self,
    ) -> None:
        app = self._app()
        files = [DeclaredFile(ref=_ref(e)) for e in ("database", "table")]

        with (
            self._patch_upload(app) as upload,
            mock.patch.object(
                app, "_verify_refs_impl", new_callable=mock.AsyncMock
            ) as verify,
        ):
            out = await app.upload_refs(
                UploadRefsInput(files=files, source_prefix=SOURCE, prefix=DEST)
            )

        keys = [c.args[0].storage_path for c in upload.await_args_list]
        assert keys == [
            f"{DEST}/database/entities.json",
            f"{DEST}/table/entities.json",
        ]
        # Every call carries the ref, so the SDK can stream from the deployment
        # store for files this pod never held.
        assert all(c.args[0].ref is not None for c in upload.await_args_list)
        assert out.prefix == DEST
        assert out.file_count == 2
        verify.assert_awaited_once()

    async def test_a_label_names_the_leaf(self) -> None:
        """The metabase shape: refs whose own keys carry no entity structure."""
        app = self._app()
        files = [
            DeclaredFile(ref=FileReference(storage_path="file_refs/abc.json"), label=t)
            for t in ("METABASECOLLECTION", "METABASEQUESTION")
        ]

        with (
            self._patch_upload(app) as upload,
            mock.patch.object(app, "_verify_refs_impl", new_callable=mock.AsyncMock),
        ):
            await app.upload_refs(UploadRefsInput(files=files, prefix=DEST))

        keys = [c.args[0].storage_path for c in upload.await_args_list]
        assert keys == [f"{DEST}/METABASECOLLECTION", f"{DEST}/METABASEQUESTION"]

    async def test_one_ref_keeps_the_same_shape_as_four(self) -> None:
        """A rule that recovers the entity segment from four refs and drops it
        from one delivers a differently-shaped tree on exactly the small runs
        nobody inspects. ``source_prefix`` is count-independent by design."""
        app = self._app()

        with (
            self._patch_upload(app) as upload,
            mock.patch.object(app, "_verify_refs_impl", new_callable=mock.AsyncMock),
        ):
            await app.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=_ref("database"))],
                    source_prefix=SOURCE,
                    prefix=DEST,
                )
            )

        assert upload.await_args.args[0].storage_path == (
            f"{DEST}/database/entities.json"
        )

    async def test_a_file_with_no_label_and_no_matching_source_prefix_raises(
        self,
    ) -> None:
        app = self._app()

        with (
            self._patch_upload(app),
            mock.patch.object(app, "_verify_refs_impl", new_callable=mock.AsyncMock),
            pytest.raises(UnplaceableDeclaredFileError) as exc,
        ):
            await app.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=_ref("database", prefix="somewhere/else"))],
                    source_prefix=SOURCE,
                    prefix=DEST,
                )
            )

        from application_sdk.errors.categories import Audience

        assert exc.value.audience is Audience.APP_OWNER

    async def test_every_upload_opts_in_to_raise_on_empty(self) -> None:
        """A declared file that contributes zero objects is a hole in the tree
        the consumer will walk, not a quiet day."""
        app = self._app()

        with (
            self._patch_upload(app) as upload,
            mock.patch.object(app, "_verify_refs_impl", new_callable=mock.AsyncMock),
        ):
            await app.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=_ref("database"))],
                    source_prefix=SOURCE,
                    prefix=DEST,
                )
            )

        assert upload.await_args.args[0].raise_on_empty is True
        assert upload.await_args.args[0].tier is StorageTier.RETAINED

    async def test_an_empty_declaration_returns_an_empty_prefix(self) -> None:
        """An empty tree is not a no-op to a consumer that diffs against it —
        it is "delete everything". The caller must hand on ``""``, not a prefix
        naming nothing."""
        app = self._app()

        with (
            self._patch_upload(app) as upload,
            mock.patch.object(
                app, "_verify_refs_impl", new_callable=mock.AsyncMock
            ) as verify,
        ):
            out = await app.upload_refs(UploadRefsInput(files=[], prefix=DEST))

        assert out.prefix == ""
        assert out.refs == []
        upload.assert_not_awaited()
        verify.assert_not_awaited()

    async def test_the_delivery_is_verified_in_the_store_it_landed_in(self) -> None:
        app = self._app()

        with (
            self._patch_upload(app),
            mock.patch.object(
                app, "_verify_refs_impl", new_callable=mock.AsyncMock
            ) as verify,
        ):
            await app.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=_ref("database"))],
                    source_prefix=SOURCE,
                    prefix=DEST,
                )
            )

        sent = verify.await_args.args[0]
        assert sent.store is StoreTarget.UPSTREAM
        assert sent.prefix == DEST
        assert sent.refs[0].storage_path == f"{DEST}/database/entities.json"
        # A HEAD, not a download of everything just delivered.
        assert sent.refs[0].auto_materialize is False

    async def test_verification_can_be_turned_off(self) -> None:
        app = self._app()

        with (
            self._patch_upload(app),
            mock.patch.object(
                app, "_verify_refs_impl", new_callable=mock.AsyncMock
            ) as verify,
        ):
            await app.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=_ref("database"))],
                    source_prefix=SOURCE,
                    prefix=DEST,
                    verify=False,
                )
            )

        verify.assert_not_awaited()

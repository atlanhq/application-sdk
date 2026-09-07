"""FND-1790: App.verify_refs checks a declaration, not a prefix listing.

``SqlApp.run()`` used to discard the four ``FileReference`` values its
transform tasks returned and hand publish a string-joined prefix instead.  A
prefix is walked, and a walk cannot tell "absent" from "lost" — a three-of-four
``transformed/`` tree reads exactly like a run that only had three entities, so
publish diffed the tenant against the subset and archived the rest.  These
tests pin the assertion that closes that gap.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import VerifyRefsInput
from application_sdk.contracts.types import FileReference, StoreTarget
from application_sdk.storage.errors import StorageHandoffIncompleteError

PREFIX = "artifacts/apps/a/workflows/wf-1/run-1/transformed"


class _In(Input, allow_unbounded_fields=True):
    pass


class _Out(Output, allow_unbounded_fields=True):
    pass


class _VerifyApp(App):
    async def run(self, input: _In) -> _Out:
        return _Out()


def _ref(entity: str, *, file_count: int = 1) -> FileReference:
    return FileReference(
        local_path=f"/tmp/transformed/{entity}/entities.json",
        storage_path=f"{PREFIX}/{entity}/entities.json",
        is_durable=True,
        file_count=file_count,
    )


class TestVerifyRefs:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _app(self, *, upstream: object | None = None) -> App:
        from application_sdk.app.context import AppContext

        app = _VerifyApp()
        app._context = AppContext(
            app_name=app._app_name,
            app_version="1",
            run_id="run-1",
            _storage=object(),
            _upstream_storage=upstream,  # type: ignore[arg-type]
        )
        return app

    async def test_all_present_returns_the_counts(self) -> None:
        app = self._app()
        refs = [_ref(e) for e in ("database", "schema", "table", "column")]

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ) as head:
            out = await app.verify_refs(VerifyRefsInput(refs=refs, prefix=PREFIX))

        assert out.verified_count == 4
        assert out.verified_file_count == 4
        assert out.prefix == PREFIX
        assert head.await_count == 4

    async def test_a_missing_object_fails_the_run_and_names_the_key(self) -> None:
        """The exact production shape: three of four entities reached the store.

        A listing of ``transformed/`` here returns three keys and looks healthy.
        Checking the declaration is the only thing that can tell the difference.
        """
        app = self._app()
        refs = [_ref(e) for e in ("database", "schema", "table", "column")]
        lost = f"{PREFIX}/table/entities.json"

        async def _exists(key: str, store=None, *, normalize: bool = True) -> bool:
            return key != lost

        with (
            mock.patch("application_sdk.storage.ops.exists", side_effect=_exists),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(VerifyRefsInput(refs=refs, prefix=PREFIX))

        assert exc.value.missing_keys == [lost]
        assert exc.value.declared_count == 4
        assert exc.value.effective_retryable is False

    async def test_a_ref_outside_the_prefix_fails_without_a_lookup(self) -> None:
        """Present in the store but not under the prefix is the same hole.

        A consumer walking ``prefix`` never reaches it, so verifying only
        existence would pass a handoff that is short at read time.
        """
        app = self._app()
        stray = FileReference(
            storage_path="artifacts/apps/a/workflows/wf-1/run-1/raw/table/records.json",
            is_durable=True,
        )

        with (
            mock.patch(
                "application_sdk.storage.ops.exists",
                new_callable=mock.AsyncMock,
                return_value=True,
            ) as head,
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(
                VerifyRefsInput(refs=[_ref("database"), stray], prefix=PREFIX)
            )

        assert exc.value.outside_prefix_keys == [stray.storage_path]
        assert head.await_count == 1  # only the in-prefix ref was looked up

    async def test_a_ref_with_no_storage_path_counts_as_missing(self) -> None:
        """A producer that cannot say where it wrote has declared nothing."""
        app = self._app()

        with (
            mock.patch(
                "application_sdk.storage.ops.exists",
                new_callable=mock.AsyncMock,
                return_value=True,
            ),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(
                VerifyRefsInput(refs=[FileReference(local_path="/tmp/x.json")])
            )

        assert exc.value.missing_keys == ["<no storage_path>"]

    async def test_directory_ref_is_checked_by_listing_not_by_head(self) -> None:
        app = self._app()
        ref = _ref("table", file_count=3)

        with mock.patch(
            "application_sdk.storage.batch.list_keys",
            new_callable=mock.AsyncMock,
            return_value=[f"{ref.storage_path}/{i}.json" for i in range(3)],
        ) as listing:
            out = await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert out.verified_file_count == 3
        assert listing.await_count == 1

    async def test_short_directory_ref_fails_with_the_observed_count(self) -> None:
        app = self._app()
        ref = _ref("table", file_count=3)

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=[f"{ref.storage_path}/0.json"],
            ),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert exc.value.missing_keys == [f"{ref.storage_path} (1/3 objects)"]

    async def test_keys_are_not_renormalised_before_the_lookup(self) -> None:
        """``persist_file_reference`` writes with ``normalize=False``.

        Re-normalising here would check a different key than the one written,
        which is a check that can pass while the object is not where the
        declaration says it is.
        """
        app = self._app()

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ) as head:
            await app.verify_refs(VerifyRefsInput(refs=[_ref("database")]))

        assert head.await_args.kwargs["normalize"] is False

    async def test_deployment_store_is_the_default_target(self) -> None:
        """The interceptor persists refs to the deployment store, so that is
        the store a task-to-task declaration must be asserted against — even
        in an SDR deployment where ``App.upload`` routes elsewhere."""
        upstream = object()
        app = self._app(upstream=upstream)

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ) as head:
            await app.verify_refs(VerifyRefsInput(refs=[_ref("database")]))

        assert head.await_args.kwargs["store"] is app.context.storage

    async def test_upstream_target_checks_the_upstream_store(self) -> None:
        upstream = object()
        app = self._app(upstream=upstream)

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ) as head:
            await app.verify_refs(
                VerifyRefsInput(refs=[_ref("database")], store=StoreTarget.UPSTREAM)
            )

        assert head.await_args.kwargs["store"] is upstream

    async def test_an_empty_declaration_verifies_vacuously(self) -> None:
        app = self._app()

        with mock.patch(
            "application_sdk.storage.ops.exists", new_callable=mock.AsyncMock
        ) as head:
            out = await app.verify_refs(VerifyRefsInput(refs=[], prefix=PREFIX))

        assert out.verified_count == 0
        head.assert_not_awaited()

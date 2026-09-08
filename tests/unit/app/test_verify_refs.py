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
from tests.unit.conftest import RecordingProgressTracker

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


def _dir_ref(entity: str, *, file_count: int, slash: bool = True) -> FileReference:
    """A directory ref, in the shape ``persist_file_reference`` writes one.

    Its directory branch keys off ``_make_storage_prefix``, which always ends
    in ``/``. Pass ``slash=False`` for the other shape in the wild: a directory
    key pinned by hand, where ``file_count`` is the only signal left.
    """
    return FileReference(
        local_path=f"/tmp/transformed/{entity}",
        storage_path=f"{PREFIX}/{entity}" + ("/" if slash else ""),
        is_durable=True,
        file_count=file_count,
    )


def _make_app(*, upstream: object | None = None) -> App:
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


class _ResetsRegistries:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()


class TestVerifyRefs(_ResetsRegistries):
    async def test_all_present_returns_the_counts(self) -> None:
        app = _make_app()
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
        app = _make_app()
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
        app = _make_app()
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
        app = _make_app()

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
        app = _make_app()
        ref = _dir_ref("table", file_count=3)

        with mock.patch(
            "application_sdk.storage.batch.list_keys",
            new_callable=mock.AsyncMock,
            return_value=[f"{PREFIX}/table/{i}.json" for i in range(3)],
        ) as listing:
            out = await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert out.verified_file_count == 3
        assert listing.await_count == 1

    async def test_short_directory_ref_fails_with_the_observed_count(self) -> None:
        app = _make_app()
        ref = _dir_ref("table", file_count=3)

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=[f"{PREFIX}/table/0.json"],
            ),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert exc.value.missing_keys == [f"{PREFIX}/table/ (1/3 objects)"]

    async def test_single_file_directory_ref_is_listed_not_headed(self) -> None:
        """A directory holding exactly one file has ``file_count == 1``.

        Discriminating on ``file_count > 1`` alone HEADs its key as an object,
        which 404s on a prefix that is perfectly intact — so the run fails
        claiming data is lost when nothing is. The trailing slash the
        interceptor writes is what distinguishes them.
        """
        app = _make_app()
        ref = _dir_ref("table", file_count=1)

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=[f"{PREFIX}/table/only.json"],
            ) as listing,
            mock.patch(
                "application_sdk.storage.ops.exists", new_callable=mock.AsyncMock
            ) as head,
        ):
            out = await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert out.verified_count == 1
        assert listing.await_count == 1
        head.assert_not_awaited()

    async def test_slashless_directory_ref_is_caught_by_its_file_count(self) -> None:
        """The other signal: a directory key pinned by hand, with no slash."""
        app = _make_app()
        ref = _dir_ref("table", file_count=2, slash=False)

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=[f"{PREFIX}/table/{i}.json" for i in range(2)],
            ) as listing,
            mock.patch(
                "application_sdk.storage.ops.exists", new_callable=mock.AsyncMock
            ) as head,
        ):
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert listing.await_count == 1
        head.assert_not_awaited()

    async def test_sidecars_do_not_count_toward_the_declared_file_count(self) -> None:
        """Every uploaded object carries a ``{key}.sha256`` sidecar.

        Counting a raw listing sees roughly 2N keys for N files, so a tree
        missing half its data objects clears ``file_count`` and the check
        passes on exactly the shortfall it exists to catch.
        """
        app = _make_app()
        ref = _dir_ref("table", file_count=4)
        # Two data objects, each with a sidecar: 4 keys, but only 2 files.
        listing = [
            f"{PREFIX}/table/{i}.json{suffix}"
            for i in range(2)
            for suffix in ("", ".sha256")
        ]

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=listing,
            ),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert exc.value.missing_keys == [f"{PREFIX}/table/ (2/4 objects)"]

    async def test_the_listing_prefix_carries_a_trailing_slash(self) -> None:
        """Without it the listing bleeds into sibling directories.

        ``list_keys`` only appends the slash when ``normalize=True``, and this
        call passes ``normalize=False`` to match the writer's key — so
        ``transformed/table`` would also match ``transformed/table_v2/`` and
        count its objects toward this ref.
        """
        app = _make_app()
        ref = _dir_ref("table", file_count=1)

        with mock.patch(
            "application_sdk.storage.batch.list_keys",
            new_callable=mock.AsyncMock,
            return_value=[f"{PREFIX}/table/only.json"],
        ) as listing:
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert listing.await_args.args[0] == f"{PREFIX}/table/"
        assert listing.await_args.kwargs["normalize"] is False

    async def test_a_directory_ref_declaring_zero_files_is_not_a_pass(self) -> None:
        """``len(found) < 0`` is false for every listing, so a zero-count ref
        would otherwise clear the check without inspecting anything — and a
        vacuous pass is indistinguishable from a real one."""
        app = _make_app()
        ref = _dir_ref("table", file_count=0)

        with (
            mock.patch(
                "application_sdk.storage.batch.list_keys",
                new_callable=mock.AsyncMock,
                return_value=[],
            ),
            pytest.raises(StorageHandoffIncompleteError) as exc,
        ):
            await app.verify_refs(VerifyRefsInput(refs=[ref], prefix=PREFIX))

        assert exc.value.missing_keys == [f"{PREFIX}/table/ (declared 0 files)"]

    async def test_keys_are_not_renormalised_before_the_lookup(self) -> None:
        """``persist_file_reference`` writes with ``normalize=False``.

        Re-normalising here would check a different key than the one written,
        which is a check that can pass while the object is not where the
        declaration says it is.
        """
        app = _make_app()

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
        app = _make_app(upstream=upstream)

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ) as head:
            await app.verify_refs(VerifyRefsInput(refs=[_ref("database")]))

        assert head.await_args.kwargs["store"] is app.context.storage

    async def test_upstream_target_checks_the_upstream_store(self) -> None:
        upstream = object()
        app = _make_app(upstream=upstream)

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
        app = _make_app()

        with mock.patch(
            "application_sdk.storage.ops.exists", new_callable=mock.AsyncMock
        ) as head:
            out = await app.verify_refs(VerifyRefsInput(refs=[], prefix=PREFIX))

        assert out.verified_count == 0
        head.assert_not_awaited()


class TestVerifyRefsFeedsTheStallWatchdog(_ResetsRegistries):
    """``verify_refs`` takes the ADR-0018 backstop instead of a duration budget
    (pinned in ``test_framework_task_timeouts.py``), which is only safe because
    the loop emits. These are the hooks that make it emit."""

    async def test_each_verified_ref_marks_progress(
        self, progress_marks: RecordingProgressTracker
    ) -> None:
        """One mark per store round-trip — a file boundary, not a record one.

        Without it the loop is a run of bare awaits with no signal, so a wedged
        HEAD against an unreachable store would hold silently all the way to the
        24h backstop rather than being caught by the watchdog in minutes.
        """
        app = _make_app()
        refs = [_ref(e) for e in ("database", "schema", "table", "column")]

        with mock.patch(
            "application_sdk.storage.ops.exists",
            new_callable=mock.AsyncMock,
            return_value=True,
        ):
            await app.verify_refs(VerifyRefsInput(refs=refs, prefix=PREFIX))

        assert progress_marks.count("storage.verify_ref") == 4

    async def test_a_ref_that_fails_the_check_marks_nothing(
        self, progress_marks: RecordingProgressTracker
    ) -> None:
        """A signal for work that did not complete would let a store returning
        404 for everything look like steady progress."""
        app = _make_app()

        with (
            mock.patch(
                "application_sdk.storage.ops.exists",
                new_callable=mock.AsyncMock,
                return_value=False,
            ),
            pytest.raises(StorageHandoffIncompleteError),
        ):
            await app.verify_refs(
                VerifyRefsInput(refs=[_ref("database")], prefix=PREFIX)
            )

        assert progress_marks.count("storage.verify_ref") == 0

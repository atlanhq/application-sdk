"""FND-3795: a ref ``App.download`` / ``App.upload`` returns names its store.

In a two-store (SDR) deployment ``App.download`` reads the upstream store, but
the activity interceptor materialises a task's input refs from the deployment
store. A directory ref handed from ``download()`` to the next task was listed
in the wrong bucket, found empty, and failed AAF-STR-009 on every attempt —
before #4318 the same empty listing returned the ref unchanged, which only
worked because both tasks happened to run in one container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import DownloadInput, UploadInput
from application_sdk.contracts.types import FileReference, StoreTarget
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import materialize_file_refs
from application_sdk.storage.ops import _put

PREFIX = "artifacts/apps/qi/workflows/wf-1/run-1/view-lineage"
_FILES = {"part-0.json": b'{"a": 1}\n', "part-1.json": b'{"b": 2}\n'}


class _In(Input, allow_unbounded_fields=True):
    pass


class _Out(Output, allow_unbounded_fields=True):
    pass


class _BuildLineageInput(Input):
    """A downstream task's input, in the shape the failing connector used."""

    qi_records: FileReference


class _TransferApp(App):
    async def run(self, input: _In) -> _Out:
        return _Out()


def _make_app(deployment: object, upstream: object | None) -> App:
    from application_sdk.app.context import AppContext

    app = _TransferApp()
    app._context = AppContext(
        app_name=app._app_name,
        app_version="1",
        run_id="run-1",
        _storage=deployment,  # type: ignore[arg-type]
        _upstream_storage=upstream,  # type: ignore[arg-type]
    )
    return app


async def _seed(store: object) -> None:
    """Write a producer's output the way another app (QI) leaves it: no manifest."""
    for name, data in _FILES.items():
        await _put(f"{PREFIX}/{name}", data, store)  # type: ignore[arg-type]


def _names(root: str | None) -> set[str]:
    assert root is not None
    return {
        p.name
        for p in Path(root).rglob("*")
        if p.is_file() and not p.name.endswith(".sha256")
    }


@pytest.fixture(autouse=True)
def _reset_registries():
    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()


class TestDownloadHandoffAcrossStores:
    async def test_two_store_download_then_materialize_on_another_worker(
        self, tmp_path: Path
    ) -> None:
        """The production failure, end to end: download upstream → next task.

        Without ``store`` on the ref the interceptor lists the deployment store,
        sees nothing, and raises AAF-STR-009 though the files are upstream.
        """
        deployment, upstream = create_memory_store(), create_memory_store()
        await _seed(upstream)
        app = _make_app(deployment, upstream)

        dl = await app.download(DownloadInput(storage_path=PREFIX))
        assert dl.ref.file_count == 2
        assert dl.ref.store is StoreTarget.UPSTREAM

        # The next task runs on a worker that does not have download()'s temp dir.
        elsewhere = dl.ref.model_copy(update={"local_path": str(tmp_path / "other")})
        out = await materialize_file_refs(
            deployment,
            _BuildLineageInput(qi_records=elsewhere),
            upstream_store=upstream,
        )

        assert _names(out.qi_records.local_path) == set(_FILES)

    async def test_two_store_download_then_materialize_on_the_same_worker(
        self,
    ) -> None:
        """The e2e shape: one container, ``local_path`` still holds both files."""
        deployment, upstream = create_memory_store(), create_memory_store()
        await _seed(upstream)
        app = _make_app(deployment, upstream)

        dl = await app.download(DownloadInput(storage_path=PREFIX))
        out = await materialize_file_refs(
            deployment, _BuildLineageInput(qi_records=dl.ref), upstream_store=upstream
        )

        assert _names(out.qi_records.local_path) == set(_FILES)

    async def test_the_handoff_check_still_fires_for_a_deployment_ref(
        self, tmp_path: Path
    ) -> None:
        """The fix routes the read; it does not relax AAF-STR-009.

        A ref that names the deployment store, over a prefix that is empty
        there, still fails — that is the lost hand-off the check exists for.
        """
        deployment, upstream = create_memory_store(), create_memory_store()
        await _seed(upstream)
        ref = FileReference(
            local_path=str(tmp_path / "dl"),
            storage_path=f"{PREFIX}/",
            is_durable=True,
            file_count=2,
        )
        (tmp_path / "dl").mkdir()

        with pytest.raises(StorageError, match="AAF-STR-009"):
            await materialize_file_refs(
                deployment,
                _BuildLineageInput(qi_records=ref),
                upstream_store=upstream,
            )

    @pytest.mark.parametrize("wiring", ["aliased", "absent"])
    async def test_single_store_download_names_the_deployment_store(
        self, wiring: str
    ) -> None:
        """In-cluster aliases one store under both names; local/CI binds no upstream."""
        deployment = create_memory_store()
        await _seed(deployment)
        app = _make_app(deployment, deployment if wiring == "aliased" else None)

        dl = await app.download(DownloadInput(storage_path=PREFIX))
        assert dl.ref.store is StoreTarget.DEPLOYMENT

        out = await materialize_file_refs(
            deployment,
            _BuildLineageInput(qi_records=dl.ref),
            upstream_store=app.context.upstream_storage,
        )
        assert _names(out.qi_records.local_path) == set(_FILES)


class TestUploadNamesItsStore:
    async def test_upload_to_a_distinct_upstream_names_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED", False
        )
        deployment, upstream = create_memory_store(), create_memory_store()
        src = tmp_path / "out"
        src.mkdir()
        for name, data in _FILES.items():
            (src / name).write_bytes(data)
        app = _make_app(deployment, upstream)

        up = await app.upload(UploadInput(local_path=str(src)))

        assert up.ref.store is StoreTarget.UPSTREAM
        out = await materialize_file_refs(
            deployment,
            _BuildLineageInput(
                qi_records=up.ref.model_copy(
                    update={"local_path": str(tmp_path / "other")}
                )
            ),
            upstream_store=upstream,
        )
        assert _names(out.qi_records.local_path) == set(_FILES)

    async def test_upload_with_no_upstream_names_the_deployment_store(
        self, tmp_path: Path
    ) -> None:
        deployment = create_memory_store()
        src = tmp_path / "out"
        src.mkdir()
        (src / "part-0.json").write_bytes(b"{}\n")
        app = _make_app(deployment, None)

        up = await app.upload(UploadInput(local_path=str(src)))

        assert up.ref.store is StoreTarget.DEPLOYMENT


class TestStoreFieldOnTheWire:
    def test_a_payload_without_the_field_reads_as_deployment(self) -> None:
        """What a producer on an older SDK sends: no ``store`` key at all."""
        ref = FileReference.model_validate(
            {"storage_path": f"{PREFIX}/", "is_durable": True, "file_count": 2}
        )
        assert ref.store is StoreTarget.DEPLOYMENT

    def test_round_trips(self) -> None:
        ref = FileReference(storage_path=PREFIX, store=StoreTarget.UPSTREAM)
        assert FileReference.model_validate_json(ref.model_dump_json()) == ref

    async def test_an_upstream_ref_with_no_upstream_bound_reads_deployment(
        self, tmp_path: Path
    ) -> None:
        """Mirrors ``App.download``'s routing: no upstream binding → deployment."""
        deployment = create_memory_store()
        await _seed(deployment)
        ref = FileReference(
            local_path=str(tmp_path / "dl"),
            storage_path=f"{PREFIX}/",
            is_durable=True,
            file_count=2,
            store=StoreTarget.UPSTREAM,
        )

        out = await materialize_file_refs(
            deployment, _BuildLineageInput(qi_records=ref), upstream_store=None
        )

        assert _names(out.qi_records.local_path) == set(_FILES)

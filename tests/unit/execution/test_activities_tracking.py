"""Tests for FileReference tracking in create_activity_from_task()."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

from application_sdk.app.base import _app_state, _app_state_lock
from application_sdk.constants import TRACKED_FILE_REFS_KEY
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.execution._temporal.activities import _track_file_refs


class _TrackIn(Input, allow_unbounded_fields=True):
    ref: FileReference | None = None


class _TrackOut(Output, allow_unbounded_fields=True):
    out: FileReference | None = None


@dataclass
class _RefInput:
    local_path: str = ""
    storage_path: str = ""


class TestTrackFileRefs:
    """Tests for the _track_file_refs() helper."""

    def setup_method(self) -> None:
        with _app_state_lock:
            _app_state.clear()

    def teardown_method(self) -> None:
        with _app_state_lock:
            _app_state.clear()

    def test_adds_ref_to_app_state(self) -> None:
        ref = FileReference(local_path="/tmp/out.parquet", storage_path="artifacts/x")
        _track_file_refs(("wf-add", "run-1"), ref)

        with _app_state_lock:
            tracked = _app_state.get(("wf-add", "run-1"), {}).get(TRACKED_FILE_REFS_KEY)

        assert tracked is not None
        assert ref in tracked

    def test_accumulates_refs_across_calls(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.parquet", storage_path="artifacts/a")
        ref2 = FileReference(local_path="/tmp/b.parquet", storage_path="artifacts/b")
        _track_file_refs(("wf-accum", "run-1"), ref1)
        _track_file_refs(("wf-accum", "run-1"), ref2)

        with _app_state_lock:
            tracked = _app_state.get(("wf-accum", "run-1"), {}).get(
                TRACKED_FILE_REFS_KEY
            )

        assert ref1 in tracked
        assert ref2 in tracked

    def test_deduplicates_identical_refs(self) -> None:
        ref = FileReference(local_path="/tmp/c.parquet", storage_path="artifacts/c")
        _track_file_refs(("wf-dedup", "run-1"), ref)
        _track_file_refs(("wf-dedup", "run-1"), ref)

        with _app_state_lock:
            tracked = _app_state.get(("wf-dedup", "run-1"), {}).get(
                TRACKED_FILE_REFS_KEY
            )

        assert len(tracked) == 1

    def test_noop_when_no_refs(self) -> None:
        _track_file_refs(("wf-noop", "run-1"))

        with _app_state_lock:
            assert ("wf-noop", "run-1") not in _app_state

    def test_isolates_by_workflow_id(self) -> None:
        ref1 = FileReference(local_path="/tmp/x.parquet", storage_path="x")
        ref2 = FileReference(local_path="/tmp/y.parquet", storage_path="y")
        _track_file_refs(("wf-iso-a", "run-1"), ref1)
        _track_file_refs(("wf-iso-b", "run-1"), ref2)

        with _app_state_lock:
            tracked_a = _app_state.get(("wf-iso-a", "run-1"), {}).get(
                TRACKED_FILE_REFS_KEY, set()
            )
            tracked_b = _app_state.get(("wf-iso-b", "run-1"), {}).get(
                TRACKED_FILE_REFS_KEY, set()
            )

        assert ref1 in tracked_a and ref2 not in tracked_a
        assert ref2 in tracked_b and ref1 not in tracked_b

    def test_isolates_runs_of_the_same_workflow(self) -> None:
        ref1 = FileReference(local_path="/tmp/r1.parquet", storage_path="r1")
        ref2 = FileReference(local_path="/tmp/r2.parquet", storage_path="r2")
        _track_file_refs(("wf-same", "run-a"), ref1)
        _track_file_refs(("wf-same", "run-b"), ref2)

        with _app_state_lock:
            tracked_a = _app_state[("wf-same", "run-a")][TRACKED_FILE_REFS_KEY]
            tracked_b = _app_state[("wf-same", "run-b")][TRACKED_FILE_REFS_KEY]

        assert tracked_a == {ref1}
        assert tracked_b == {ref2}

    def test_task_state_is_per_run_within_one_workflow(self) -> None:
        from application_sdk.app import base as app_base

        for run, value in ((("wf-same", "run-a"), "a"), (("wf-same", "run-b"), "b")):
            with mock.patch.object(app_base, "_current_run_key", return_value=run):
                app_base.TaskStateAccessor().set("client", value)

        for run, value in ((("wf-same", "run-a"), "a"), (("wf-same", "run-b"), "b")):
            with mock.patch.object(app_base, "_current_run_key", return_value=run):
                assert app_base.TaskStateAccessor().get("client") == value

    def test_multiple_refs_in_single_call(self) -> None:
        refs = [
            FileReference(
                local_path=f"/tmp/f{i}.parquet", storage_path=f"artifacts/{i}"
            )
            for i in range(3)
        ]
        _track_file_refs(("wf-multi", "run-1"), *refs)

        with _app_state_lock:
            tracked = _app_state.get(("wf-multi", "run-1"), {}).get(
                TRACKED_FILE_REFS_KEY
            )

        for ref in refs:
            assert ref in tracked


class TestActivityTrackingWiring:
    """Tests that verify the tracking wiring in create_activity_from_task()."""

    def setup_method(self) -> None:
        with _app_state_lock:
            _app_state.clear()

    def teardown_method(self) -> None:
        with _app_state_lock:
            _app_state.clear()

    async def test_activity_tracks_input_and_result_refs_under_its_run(
        self,
    ) -> None:
        """The real activity body tracks both sides' refs under (workflow, run)."""
        import application_sdk.execution._temporal.activities as act_mod
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry
        from application_sdk.app.task import task

        in_ref = FileReference(storage_path="artifacts/in")
        out_ref = FileReference(storage_path="artifacts/out")

        class _TrackWiringApp(App):
            @task(timeout_seconds=60)
            async def produce(self, input: _TrackIn) -> _TrackOut:
                return _TrackOut(out=out_ref)

            async def run(self, input: _TrackIn) -> _TrackOut:
                return await self.produce(input)

        try:
            meta = next(
                t
                for t in TaskRegistry.get_instance().get_tasks_for_app(
                    _TrackWiringApp._app_name
                )
                if t.name == "produce"
            )
            activity_fn = act_mod.create_activity_from_task(meta)
            ctx = act_mod.TaskContext(
                app_name=_TrackWiringApp._app_name,
                task_name="produce",
                run_id="run-wiring",
                heartbeat_timeout_seconds=None,
                auto_heartbeat_seconds=None,
            )
            info = mock.MagicMock(workflow_id="wf-wiring", workflow_run_id="run-wiring")
            with (
                mock.patch.object(act_mod.activity, "info", return_value=info),
                mock.patch(
                    "application_sdk.infrastructure.context.get_infrastructure",
                    return_value=None,
                ),
            ):
                await activity_fn(ctx, _TrackIn(ref=in_ref))
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()

        with _app_state_lock:
            assert set(_app_state) == {("wf-wiring", "run-wiring")}
            tracked = _app_state[("wf-wiring", "run-wiring")][TRACKED_FILE_REFS_KEY]
        assert {in_ref, out_ref} <= tracked

    def test_track_file_refs_import_and_exists(self) -> None:
        """_track_file_refs is importable from activities module."""
        from application_sdk.execution._temporal.activities import (
            _track_file_refs as fn,
        )

        assert callable(fn)

    def test_tracked_file_refs_key_constant_used(self) -> None:
        """TRACKED_FILE_REFS_KEY is imported in activities module."""
        import application_sdk.execution._temporal.activities as act_mod

        assert hasattr(act_mod, "TRACKED_FILE_REFS_KEY")
        assert act_mod.TRACKED_FILE_REFS_KEY == "_tracked_file_refs"

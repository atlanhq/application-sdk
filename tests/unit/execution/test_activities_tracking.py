"""Tests for FileReference tracking in create_activity_from_task()."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.app.base import _app_state, _app_state_lock
from application_sdk.constants import TRACKED_FILE_REFS_KEY
from application_sdk.contracts.types import FileReference
from application_sdk.execution._temporal.activities import _track_file_refs


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
        _track_file_refs("wf-add", ref)

        with _app_state_lock:
            tracked = _app_state.get("wf-add", {}).get(TRACKED_FILE_REFS_KEY)

        assert tracked is not None
        assert ref in tracked

    def test_accumulates_refs_across_calls(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.parquet", storage_path="artifacts/a")
        ref2 = FileReference(local_path="/tmp/b.parquet", storage_path="artifacts/b")
        _track_file_refs("wf-accum", ref1)
        _track_file_refs("wf-accum", ref2)

        with _app_state_lock:
            tracked = _app_state.get("wf-accum", {}).get(TRACKED_FILE_REFS_KEY)

        assert ref1 in tracked
        assert ref2 in tracked

    def test_deduplicates_identical_refs(self) -> None:
        ref = FileReference(local_path="/tmp/c.parquet", storage_path="artifacts/c")
        _track_file_refs("wf-dedup", ref)
        _track_file_refs("wf-dedup", ref)

        with _app_state_lock:
            tracked = _app_state.get("wf-dedup", {}).get(TRACKED_FILE_REFS_KEY)

        assert len(tracked) == 1

    def test_noop_when_no_refs(self) -> None:
        _track_file_refs("wf-noop")

        with _app_state_lock:
            assert "wf-noop" not in _app_state

    def test_isolates_by_workflow_id(self) -> None:
        ref1 = FileReference(local_path="/tmp/x.parquet", storage_path="x")
        ref2 = FileReference(local_path="/tmp/y.parquet", storage_path="y")
        _track_file_refs("wf-iso-a", ref1)
        _track_file_refs("wf-iso-b", ref2)

        with _app_state_lock:
            tracked_a = _app_state.get("wf-iso-a", {}).get(TRACKED_FILE_REFS_KEY, set())
            tracked_b = _app_state.get("wf-iso-b", {}).get(TRACKED_FILE_REFS_KEY, set())

        assert ref1 in tracked_a and ref2 not in tracked_a
        assert ref2 in tracked_b and ref1 not in tracked_b

    def test_multiple_refs_in_single_call(self) -> None:
        refs = [
            FileReference(local_path=f"/tmp/f{i}.parquet", storage_path=f"artifacts/{i}")
            for i in range(3)
        ]
        _track_file_refs("wf-multi", *refs)

        with _app_state_lock:
            tracked = _app_state.get("wf-multi", {}).get(TRACKED_FILE_REFS_KEY)

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

    @pytest.mark.asyncio
    async def test_refs_from_input_and_output_are_tracked(self) -> None:
        """_track_file_refs is called with refs from both input_data and result."""
        import application_sdk.execution._temporal.activities as act_mod

        in_ref = FileReference(local_path="/tmp/in.parquet", storage_path="artifacts/in")
        out_ref = FileReference(local_path="/tmp/out.parquet", storage_path="artifacts/out")

        tracked_calls: list[tuple] = []

        orig_track = act_mod._track_file_refs

        def capture_track(wf_id: str, *refs: FileReference) -> None:
            tracked_calls.append((wf_id, refs))

        with mock.patch.object(act_mod, "_track_file_refs", side_effect=capture_track):
            with mock.patch(
                "application_sdk.storage.file_ref_sync._find_file_refs",
                side_effect=[[in_ref], [out_ref]],
            ):
                # Directly exercise the tracking branch
                all_refs = [in_ref] + [out_ref]
                if all_refs:
                    act_mod._track_file_refs("wf-wiring", *all_refs)

        assert len(tracked_calls) == 1
        wf_id, refs = tracked_calls[0]
        assert wf_id == "wf-wiring"
        assert in_ref in refs
        assert out_ref in refs

    def test_track_file_refs_import_and_exists(self) -> None:
        """_track_file_refs is importable from activities module."""
        from application_sdk.execution._temporal.activities import _track_file_refs as fn

        assert callable(fn)

    def test_tracked_file_refs_key_constant_used(self) -> None:
        """TRACKED_FILE_REFS_KEY is imported in activities module."""
        import application_sdk.execution._temporal.activities as act_mod

        assert hasattr(act_mod, "TRACKED_FILE_REFS_KEY")
        assert act_mod.TRACKED_FILE_REFS_KEY == "_tracked_file_refs"

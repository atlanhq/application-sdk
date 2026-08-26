"""Unit tests for input sizing — the driver variable for tier fitting."""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from application_sdk.contracts.types import FileReference
from application_sdk.observability.sizing_inputs import (
    InputSize,
    _walk,
    begin_collection,
    describe_inputs,
    end_collection,
    report_input_bytes,
    report_local_paths,
)


class _Nested(BaseModel):
    ref: FileReference | None = None


class _Model(BaseModel):
    name: str = "x"
    ref: FileReference | None = None
    refs: list[FileReference] = []
    nested: _Nested | None = None


def _file(tmp_path, name: str, size: int) -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return str(p)


class TestFileReferenceSizing:
    def test_single_file(self, tmp_path):
        model = _Model(ref=FileReference(local_path=_file(tmp_path, "a.parquet", 500)))
        got = describe_inputs(model)
        assert got == InputSize(bytes=500, file_count=1, basis="file_reference")

    def test_directory_is_walked(self, tmp_path):
        _file(tmp_path, "d/1.parquet", 100)
        _file(tmp_path, "d/2.parquet", 250)
        _file(tmp_path, "d/sub/3.parquet", 50)
        model = _Model(ref=FileReference(local_path=str(tmp_path / "d")))
        got = describe_inputs(model)
        assert got is not None
        assert got.bytes == 400
        assert got.file_count == 3

    def test_sums_across_fields_and_nesting(self, tmp_path):
        """merge-shaped inputs put several refs in a list, sometimes nested."""
        model = _Model(
            ref=FileReference(local_path=_file(tmp_path, "a", 10)),
            refs=[
                FileReference(local_path=_file(tmp_path, "b", 20)),
                FileReference(local_path=_file(tmp_path, "c", 30)),
            ],
            nested=_Nested(ref=FileReference(local_path=_file(tmp_path, "d", 40))),
        )
        got = describe_inputs(model)
        assert got is not None
        assert got.bytes == 100
        assert got.file_count == 4

    def test_unmaterialised_ref_is_skipped(self):
        """``auto_materialize=False`` leaves no local path."""
        model = _Model(
            ref=FileReference(storage_path="s3://bucket/key", is_durable=True)
        )
        assert describe_inputs(model) is None

    def test_no_refs_returns_none(self):
        assert describe_inputs(_Model()) is None

    def test_missing_path_is_none_not_zero(self, tmp_path):
        """A ref pointing at nothing is unknown; 0 would fit a rule to nothing."""
        model = _Model(ref=FileReference(local_path=str(tmp_path / "gone")))
        assert describe_inputs(model) is None

    def test_walk_is_capped(self, tmp_path, monkeypatch):
        import application_sdk.observability.sizing_inputs as mod

        monkeypatch.setattr(mod, "_MAX_FILES_WALKED", 3)
        for i in range(10):
            _file(tmp_path, f"many/{i}", 10)
        total, count, hit_cap = _walk(str(tmp_path / "many"))
        assert hit_cap is True
        assert count == 3
        assert total == 30

    def test_truncation_is_reported(self, tmp_path, monkeypatch):
        """A partial count must be labelled, not silently passed off as complete."""
        import application_sdk.observability.sizing_inputs as mod

        monkeypatch.setattr(mod, "_MAX_FILES_WALKED", 2)
        for i in range(5):
            _file(tmp_path, f"d/{i}", 10)
        got = describe_inputs(_Model(ref=FileReference(local_path=str(tmp_path / "d"))))
        assert got is not None
        assert got.truncated is True


class TestReportedBytes:
    """The path AE's merge uses: readers report what they actually pulled."""

    @pytest.fixture(autouse=True)
    def collector(self):
        c = begin_collection()
        yield c
        end_collection()

    def test_reported_bytes_are_summed(self):
        report_input_bytes(1000)
        report_input_bytes(2000, file_count=3)
        got = describe_inputs(_Model())
        assert got == InputSize(bytes=3000, file_count=4, basis="reported")

    def test_reported_wins_over_file_references(self, tmp_path):
        """Reported is what the activity read; a ref is only what it was handed."""
        report_input_bytes(7)
        got = describe_inputs(
            _Model(ref=FileReference(local_path=_file(tmp_path, "a", 999)))
        )
        assert got is not None
        assert got.basis == "reported"
        assert got.bytes == 7

    def test_report_local_paths(self, tmp_path):
        paths = [_file(tmp_path, "a", 100), _file(tmp_path, "b", 250)]
        report_local_paths(paths)
        got = describe_inputs(None)
        assert got is not None
        assert got.bytes == 350
        assert got.file_count == 2

    def test_missing_path_is_skipped_not_fatal(self, tmp_path):
        report_local_paths([_file(tmp_path, "a", 100), str(tmp_path / "gone")])
        got = describe_inputs(None)
        assert got is not None
        assert got.bytes == 100
        assert got.file_count == 1

    def test_negative_report_is_ignored(self):
        report_input_bytes(-5)
        assert describe_inputs(_Model()) is None

    def test_nothing_reported_falls_back_to_refs(self, tmp_path):
        got = describe_inputs(
            _Model(ref=FileReference(local_path=_file(tmp_path, "a", 42)))
        )
        assert got is not None
        assert got.basis == "file_reference"


class TestCollectorLifecycle:
    def test_reporting_without_a_collector_is_a_no_op(self):
        """Safe to call from a hot read path when collection is disabled."""
        end_collection()
        report_input_bytes(1000)  # must not raise
        report_local_paths(["/nonexistent"])
        assert describe_inputs(_Model()) is None

    def test_end_collection_prevents_leaking_into_the_next_activity(self):
        """A worker runs activities back to back on the same context."""
        begin_collection()
        report_input_bytes(500)
        end_collection()
        assert describe_inputs(_Model()) is None

    def test_begin_collection_starts_clean(self):
        begin_collection()
        report_input_bytes(500)
        begin_collection()
        assert describe_inputs(_Model()) is None
        end_collection()


class TestRobustness:
    def test_none_input(self):
        assert describe_inputs(None) is None

    def test_primitive_input(self):
        assert describe_inputs("just a string") is None

    def test_permission_error_mid_walk_is_skipped(self, tmp_path, monkeypatch):
        """A file removed or unreadable mid-walk must not lose the whole reading."""
        _file(tmp_path, "d/a", 100)
        _file(tmp_path, "d/b", 200)
        real = os.path.getsize
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("vanished")
            return real(path)

        monkeypatch.setattr(os.path, "getsize", flaky)
        got = describe_inputs(_Model(ref=FileReference(local_path=str(tmp_path / "d"))))
        assert got is not None
        assert got.file_count == 1
        assert got.bytes in (100, 200)


class TestPeakPerInputByte:
    def test_ratio(self):
        from application_sdk.observability.sizing import SizingObservation

        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=1.0,
            peak_memory_bytes=6 * 1024**3,
            input_bytes=2 * 1024**3,
        )
        assert obs.peak_per_input_byte == pytest.approx(3.0)

    def test_none_without_an_input_size(self):
        from application_sdk.observability.sizing import SizingObservation

        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=1.0,
            peak_memory_bytes=6 * 1024**3,
        )
        assert obs.peak_per_input_byte is None


class TestReaderChokepoint:
    """The path that makes AE's merge work without AE writing any code."""

    async def test_locally_present_files_are_reported(self, tmp_path):
        from application_sdk.storage.formats.utils import _download_files

        _file(tmp_path, "p/a.parquet", 300)
        _file(tmp_path, "p/b.parquet", 700)

        begin_collection()
        try:
            found = await _download_files(str(tmp_path / "p"), ".parquet")
            assert len(found) == 2
            got = describe_inputs(None)
        finally:
            end_collection()

        assert got is not None
        assert got.bytes == 1000
        assert got.file_count == 2
        assert got.basis == "reported"

    async def test_inert_when_collection_is_off(self, tmp_path):
        """A read path must cost nothing when sizing telemetry is disabled."""
        from application_sdk.storage.formats.utils import _download_files

        _file(tmp_path, "p/a.parquet", 300)
        end_collection()
        found = await _download_files(str(tmp_path / "p"), ".parquet")
        assert len(found) == 1
        assert describe_inputs(None) is None

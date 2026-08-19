"""Unit tests for input sizing — the driver variable for tier fitting."""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from application_sdk.contracts.types import FileReference
from application_sdk.observability.sizing_inputs import (
    InputSize,
    _walk,
    describe_inputs,
)


class _Nested(BaseModel):
    ref: FileReference | None = None


class _Model(BaseModel):
    name: str = "x"
    ref: FileReference | None = None
    refs: list[FileReference] = []
    nested: _Nested | None = None


class _HookModel(BaseModel):
    prefixes: list[str] = []

    def sizing_input_bytes(self) -> int | None:
        return 4096


class _BadHookModel(BaseModel):
    def sizing_input_bytes(self):
        return "big"


class _NoneHookModel(BaseModel):
    def sizing_input_bytes(self) -> int | None:
        return None


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
        """``auto_materialize=False`` leaves no local path.

        Sizing it would need an object-store call per activity; the app that opted
        out already knows its own sizes and can use the hook.
        """
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


class TestHook:
    def test_hook_is_used_when_there_are_no_refs(self):
        """AE's merge takes raw prefixes, so refs find nothing and the hook answers."""
        got = describe_inputs(_HookModel(prefixes=["a/", "b/"]))
        assert got == InputSize(bytes=4096, file_count=0, basis="hook")

    def test_file_references_win_over_the_hook(self, tmp_path):
        """Measured beats self-reported when both are available."""

        class _Both(_Model):
            def sizing_input_bytes(self) -> int | None:
                return 999_999

        got = describe_inputs(
            _Both(ref=FileReference(local_path=_file(tmp_path, "a", 7)))
        )
        assert got is not None
        assert got.basis == "file_reference"
        assert got.bytes == 7

    def test_hook_returning_none_is_unknown(self):
        assert describe_inputs(_NoneHookModel()) is None

    def test_non_int_hook_is_rejected(self):
        """A wrong-typed hook must not poison the dataset."""
        assert describe_inputs(_BadHookModel()) is None

    def test_negative_hook_is_rejected(self):
        class _Neg(BaseModel):
            def sizing_input_bytes(self) -> int | None:
                return -1

        assert describe_inputs(_Neg()) is None

    def test_bool_is_not_an_int_here(self):
        """``True`` is an int subclass and would read as 1 byte."""

        class _Bool(BaseModel):
            def sizing_input_bytes(self):
                return True

        assert describe_inputs(_Bool()) is None

    def test_raising_hook_does_not_propagate(self):
        class _Boom(BaseModel):
            def sizing_input_bytes(self) -> int | None:
                raise RuntimeError("boom")

        assert describe_inputs(_Boom()) is None

    def test_file_count_from_companion_attribute(self):
        class _WithCount(BaseModel):
            sizing_input_file_count: int = 12

            def sizing_input_bytes(self) -> int | None:
                return 100

        got = describe_inputs(_WithCount())
        assert got is not None
        assert got.file_count == 12


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

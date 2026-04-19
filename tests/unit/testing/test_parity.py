"""Unit tests for the parity test framework."""

import json
from pathlib import Path

from application_sdk.testing.parity.comparator import (
    compare_category,
    diff_dicts,
    discover_categories,
    load_ndjson,
    run_comparison,
    strip_volatile,
)
from application_sdk.testing.parity.models import AssetDiff, CategoryResult, FieldDiff
from application_sdk.testing.parity.report import (
    generate_json_report,
    generate_markdown,
)

# ---------------------------------------------------------------------------
# strip_volatile
# ---------------------------------------------------------------------------


class TestStripVolatile:
    def test_removes_volatile_fields(self):
        obj = {"name": "t1", "lastSyncRun": "run-1", "status": "ACTIVE"}
        result = strip_volatile(obj)
        assert result == {"name": "t1", "status": "ACTIVE"}

    def test_nested_dict(self):
        obj = {"a": {"lastSyncRunAt": "2026-01-01", "b": 1}}
        result = strip_volatile(obj)
        assert result == {"a": {"b": 1}}

    def test_list_of_dicts(self):
        obj = [{"lastSyncRun": "x", "keep": True}, {"keep": False}]
        result = strip_volatile(obj)
        assert result == [{"keep": True}, {"keep": False}]

    def test_passthrough_scalar(self):
        assert strip_volatile(42) == 42
        assert strip_volatile("hello") == "hello"
        assert strip_volatile(None) is None


# ---------------------------------------------------------------------------
# load_ndjson
# ---------------------------------------------------------------------------


class TestLoadNdjson:
    def test_loads_valid_ndjson(self, tmp_path):
        f = tmp_path / "chunk-0.json"
        f.write_text(
            '{"typeName":"Table","attributes":{"qualifiedName":"t1"}}\n'
            '{"typeName":"Table","attributes":{"qualifiedName":"t2"}}\n',
            encoding="utf-8",
        )
        assets = load_ndjson(tmp_path)
        assert len(assets) == 2
        assert assets[0]["attributes"]["qualifiedName"] == "t1"

    def test_skips_statistics_files(self, tmp_path):
        (tmp_path / "statistics.json").write_text('{"count":5}\n', encoding="utf-8")
        (tmp_path / "chunk-0.json").write_text(
            '{"typeName":"Table","attributes":{"qualifiedName":"t1"}}\n',
            encoding="utf-8",
        )
        assets = load_ndjson(tmp_path)
        assert len(assets) == 1

    def test_empty_dir(self, tmp_path):
        assert load_ndjson(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert load_ndjson(tmp_path / "nope") == []

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "chunk-0.json"
        f.write_text(
            '{"valid": true}\nnot json at all\n{"also_valid": true}\n',
            encoding="utf-8",
        )
        assets = load_ndjson(tmp_path)
        assert len(assets) == 2


# ---------------------------------------------------------------------------
# diff_dicts
# ---------------------------------------------------------------------------


class TestDiffDicts:
    def test_identical(self):
        assert diff_dicts({"a": 1}, {"a": 1}) == []

    def test_value_changed(self):
        diffs = diff_dicts({"a": 1}, {"a": 2})
        assert len(diffs) == 1
        assert diffs[0].field_path == "a"
        assert diffs[0].baseline_value == 1
        assert diffs[0].candidate_value == 2

    def test_nested_diff(self):
        diffs = diff_dicts({"a": {"b": 1}}, {"a": {"b": 2}})
        assert len(diffs) == 1
        assert diffs[0].field_path == "a.b"

    def test_added_key(self):
        diffs = diff_dicts({}, {"new": "val"})
        assert len(diffs) == 1
        assert diffs[0].field_path == "new"
        assert diffs[0].baseline_value is None

    def test_removed_key(self):
        diffs = diff_dicts({"old": "val"}, {})
        assert len(diffs) == 1
        assert diffs[0].baseline_value == "val"
        assert diffs[0].candidate_value is None


# ---------------------------------------------------------------------------
# compare_category
# ---------------------------------------------------------------------------


class TestCompareCategory:
    def _write_asset(self, directory: Path, qn: str, extra: dict | None = None):
        directory.mkdir(parents=True, exist_ok=True)
        asset = {"typeName": "Table", "attributes": {"qualifiedName": qn}}
        if extra:
            asset["attributes"].update(extra)
        f = directory / "chunk-0.json"
        mode = "a" if f.exists() else "w"
        with open(f, mode, encoding="utf-8") as fh:
            fh.write(json.dumps(asset) + "\n")

    def test_parity(self, tmp_path):
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        self._write_asset(baseline, "db/s/t1")
        self._write_asset(candidate, "db/s/t1")

        result = compare_category("table", baseline, candidate)
        assert not result.has_diffs
        assert result.baseline_count == 1
        assert result.candidate_count == 1

    def test_added(self, tmp_path):
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        baseline.mkdir()
        self._write_asset(candidate, "db/s/new")

        result = compare_category("table", baseline, candidate)
        assert len(result.added) == 1
        assert result.added[0].diff_type == "ADDED"

    def test_removed(self, tmp_path):
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        self._write_asset(baseline, "db/s/old")
        candidate.mkdir()

        result = compare_category("table", baseline, candidate)
        assert len(result.removed) == 1
        assert result.removed[0].diff_type == "REMOVED"

    def test_modified(self, tmp_path):
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        self._write_asset(baseline, "db/s/t1", {"name": "old"})
        self._write_asset(candidate, "db/s/t1", {"name": "new"})

        result = compare_category("table", baseline, candidate)
        assert len(result.modified) == 1
        assert result.modified[0].field_diffs[0].field_path == "attributes.name"


# ---------------------------------------------------------------------------
# run_comparison + discover_categories
# ---------------------------------------------------------------------------


class TestRunComparison:
    def test_discovers_categories(self, tmp_path):
        baseline = tmp_path / "baseline"
        candidate = tmp_path / "candidate"
        (baseline / "table").mkdir(parents=True)
        (baseline / "column").mkdir(parents=True)
        (candidate / "table").mkdir(parents=True)
        (candidate / "schema").mkdir(parents=True)

        cats = discover_categories(baseline, candidate)
        assert cats == ["column", "schema", "table"]

    def test_end_to_end(self, tmp_path):
        baseline = tmp_path / "baseline" / "table"
        candidate = tmp_path / "candidate" / "table"
        baseline.mkdir(parents=True)
        candidate.mkdir(parents=True)

        (baseline / "chunk-0.json").write_text(
            '{"typeName":"Table","attributes":{"qualifiedName":"t1"}}\n',
            encoding="utf-8",
        )
        (candidate / "chunk-0.json").write_text(
            '{"typeName":"Table","attributes":{"qualifiedName":"t1"}}\n',
            encoding="utf-8",
        )

        results = run_comparison(tmp_path / "baseline", tmp_path / "candidate")
        assert len(results) == 1
        assert results[0].category == "table"
        assert not results[0].has_diffs


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class TestModels:
    def test_has_diffs_true(self):
        r = CategoryResult(category="table", baseline_count=1, candidate_count=2)
        r.added.append(
            AssetDiff(qualified_name="t1", type_name="Table", diff_type="ADDED")
        )
        assert r.has_diffs is True

    def test_has_diffs_false(self):
        r = CategoryResult(category="table", baseline_count=1, candidate_count=1)
        assert r.has_diffs is False


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class TestReport:
    def _make_results(self) -> list[CategoryResult]:
        r = CategoryResult(category="table", baseline_count=2, candidate_count=3)
        r.added.append(
            AssetDiff(qualified_name="db/s/new", type_name="Table", diff_type="ADDED")
        )
        r.modified.append(
            AssetDiff(
                qualified_name="db/s/t1",
                type_name="Table",
                diff_type="MODIFIED",
                field_diffs=[
                    FieldDiff(
                        field_path="attributes.name",
                        baseline_value="old",
                        candidate_value="new",
                    )
                ],
            )
        )
        return [r]

    def test_generate_markdown_with_diffs(self):
        md = generate_markdown(self._make_results())
        assert "added" in md.lower()
        assert "modified" in md.lower()
        assert "db/s/new" in md
        assert "PARITY BROKEN" in md

    def test_generate_markdown_parity(self):
        results = [
            CategoryResult(category="table", baseline_count=1, candidate_count=1)
        ]
        md = generate_markdown(results)
        assert (
            "Parity" in md
            or "parity" in md
            or "No differences" in md.lower()
            or "✅" in md
        )

    def test_generate_json_report(self):
        report = generate_json_report(self._make_results())
        assert isinstance(report, dict)
        assert "categories" in report
        assert report["categories"][0]["category"] == "table"

"""Tests for the metadata comparison engine."""

import json
import os
import tempfile

import pytest

from application_sdk.test_utils.integration.comparison import (
    AssetDiff,
    GapReport,
    compare_metadata,
    load_actual_output,
    load_expected_data,
)


class TestCompareMetadata:
    """Tests for the compare_metadata function."""

    def test_identical_data_no_gaps(self):
        """Identical expected and actual data produces no gaps."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders", "columnCount": 6}},
                {"attributes": {"name": "users", "columnCount": 3}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders", "columnCount": 6}},
            {"typeName": "Table", "attributes": {"name": "users", "columnCount": 3}},
        ]

        report = compare_metadata(expected, actual)
        assert not report.has_gaps

    def test_missing_asset_detected(self):
        """An asset in expected but not in actual is reported as missing."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders"}},
                {"attributes": {"name": "users"}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
        ]

        report = compare_metadata(expected, actual)
        assert report.has_gaps

        missing = [d for d in report.diffs if d.diff_type == "missing"]
        assert len(missing) == 1
        assert missing[0].asset_name == "users"
        assert missing[0].asset_type == "Table"

    def test_extra_asset_strict_mode(self):
        """Extra assets in actual output fail the test in strict mode."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders"}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
            {"typeName": "Table", "attributes": {"name": "extra_table"}},
        ]

        report = compare_metadata(expected, actual, strict=True)
        assert report.has_gaps

        extra = [d for d in report.diffs if d.diff_type == "extra"]
        assert len(extra) == 1
        assert extra[0].asset_name == "extra_table"

    def test_extra_asset_lenient_mode(self):
        """Extra assets in actual output are ignored in lenient mode."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders"}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
            {"typeName": "Table", "attributes": {"name": "extra_table"}},
        ]

        report = compare_metadata(expected, actual, strict=False)
        # Only count_mismatch, no "extra" diffs
        extra = [d for d in report.diffs if d.diff_type == "extra"]
        assert len(extra) == 0

    def test_attribute_mismatch_detected(self):
        """Differing attribute values are reported as attribute_mismatch."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders", "columnCount": 6}},
            ]
        }
        actual = [
            {
                "typeName": "Table",
                "attributes": {"name": "orders", "columnCount": 10},
            },
        ]

        report = compare_metadata(expected, actual)
        assert report.has_gaps

        mismatches = [d for d in report.diffs if d.diff_type == "attribute_mismatch"]
        assert len(mismatches) == 1
        assert mismatches[0].field == "attributes.columnCount"
        assert mismatches[0].expected == 6
        assert mismatches[0].actual == 10

    def test_missing_attribute_detected(self):
        """An attribute in expected but absent in actual is reported."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders", "description": "Order table"}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
        ]

        report = compare_metadata(expected, actual)
        assert report.has_gaps

        missing_attrs = [d for d in report.diffs if d.diff_type == "missing_attribute"]
        assert len(missing_attrs) == 1
        assert missing_attrs[0].field == "attributes.description"

    def test_count_mismatch_reported(self):
        """Different asset counts are reported as count_mismatch."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders"}},
                {"attributes": {"name": "users"}},
                {"attributes": {"name": "products"}},
            ]
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
        ]

        report = compare_metadata(expected, actual)
        count_diffs = [d for d in report.diffs if d.diff_type == "count_mismatch"]
        assert len(count_diffs) == 1
        assert count_diffs[0].expected == 3
        assert count_diffs[0].actual == 1

    def test_ignored_fields_skipped(self):
        """Fields in the ignored set are not compared."""
        expected = {
            "Table": [
                {
                    "attributes": {
                        "name": "orders",
                        "qualifiedName": "old/path/orders",
                        "columnCount": 6,
                    }
                },
            ]
        }
        actual = [
            {
                "typeName": "Table",
                "attributes": {
                    "name": "orders",
                    "qualifiedName": "new/path/orders",
                    "columnCount": 6,
                },
            },
        ]

        report = compare_metadata(expected, actual)
        assert not report.has_gaps

    def test_custom_ignored_fields(self):
        """Custom ignored_fields set overrides defaults."""
        expected = {
            "Table": [
                {"attributes": {"name": "orders", "columnCount": 6}},
            ]
        }
        actual = [
            {
                "typeName": "Table",
                "attributes": {"name": "orders", "columnCount": 10},
            },
        ]

        # Ignore columnCount
        report = compare_metadata(expected, actual, ignored_fields={"columnCount"})
        assert not report.has_gaps

    def test_custom_attributes_compared(self):
        """customAttributes are compared when present in expected."""
        expected = {
            "Table": [
                {
                    "attributes": {"name": "orders"},
                    "customAttributes": {"table_type": "TABLE"},
                },
            ]
        }
        actual = [
            {
                "typeName": "Table",
                "attributes": {"name": "orders"},
                "customAttributes": {"table_type": "VIEW"},
            },
        ]

        report = compare_metadata(expected, actual)
        assert report.has_gaps
        mismatches = [d for d in report.diffs if d.diff_type == "attribute_mismatch"]
        assert len(mismatches) == 1
        assert mismatches[0].field == "customAttributes.table_type"

    def test_extra_asset_type_strict_mode(self):
        """Asset types in actual but not in expected are flagged in strict mode."""
        expected = {
            "Table": [{"attributes": {"name": "orders"}}],
        }
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
            {"typeName": "Column", "attributes": {"name": "order_id"}},
        ]

        report = compare_metadata(expected, actual, strict=True)
        extra = [
            d
            for d in report.diffs
            if d.diff_type == "extra" and d.asset_type == "Column"
        ]
        assert len(extra) == 1

    def test_nested_reference_fields_ignored(self):
        """Nested reference fields like atlanSchema are skipped by default."""
        expected = {
            "Table": [
                {
                    "attributes": {
                        "name": "orders",
                        "columnCount": 6,
                        "atlanSchema": {
                            "typeName": "Schema",
                            "uniqueAttributes": {"qualifiedName": "old/path"},
                        },
                    }
                },
            ]
        }
        actual = [
            {
                "typeName": "Table",
                "attributes": {
                    "name": "orders",
                    "columnCount": 6,
                    "atlanSchema": {
                        "typeName": "Schema",
                        "uniqueAttributes": {"qualifiedName": "new/path"},
                    },
                },
            },
        ]

        report = compare_metadata(expected, actual)
        assert not report.has_gaps

    def test_multiple_asset_types(self):
        """Comparison works across multiple asset types."""
        expected = {
            "Database": [{"attributes": {"name": "mydb"}}],
            "Table": [
                {"attributes": {"name": "orders", "columnCount": 6}},
                {"attributes": {"name": "users", "columnCount": 3}},
            ],
        }
        actual = [
            {"typeName": "Database", "attributes": {"name": "mydb"}},
            {"typeName": "Table", "attributes": {"name": "orders", "columnCount": 6}},
            {"typeName": "Table", "attributes": {"name": "users", "columnCount": 3}},
        ]

        report = compare_metadata(expected, actual)
        assert not report.has_gaps

    def test_empty_expected_data(self):
        """Empty expected data with actual assets reports extras in strict mode."""
        expected = {}
        actual = [
            {"typeName": "Table", "attributes": {"name": "orders"}},
        ]

        report = compare_metadata(expected, actual, strict=True)
        assert report.has_gaps

    def test_empty_actual_data(self):
        """Empty actual data with expected assets reports missing."""
        expected = {
            "Table": [{"attributes": {"name": "orders"}}],
        }
        actual = []

        report = compare_metadata(expected, actual)
        assert report.has_gaps
        missing = [d for d in report.diffs if d.diff_type == "missing"]
        assert len(missing) == 1


class TestGapReport:
    """Tests for GapReport formatting."""

    def test_no_gaps_message(self):
        """Empty report produces a clean message."""
        report = GapReport()
        assert "No gaps found" in report.format_report()

    def test_format_report_includes_summary(self):
        """Report includes summary counts."""
        report = GapReport(
            diffs=[
                AssetDiff("Table", "orders", "missing"),
                AssetDiff("Table", "users", "extra"),
            ],
            summary={"missing": 1, "extra": 1},
        )
        output = report.format_report()
        assert "missing: 1" in output
        assert "extra: 1" in output

    def test_format_report_groups_by_type(self):
        """Report groups diffs by asset type."""
        report = GapReport(
            diffs=[
                AssetDiff("Table", "orders", "missing"),
                AssetDiff("Column", "col1", "extra"),
            ],
            summary={"missing": 1, "extra": 1},
        )
        output = report.format_report()
        assert "[Table]" in output
        assert "[Column]" in output


class TestLoadExpectedData:
    """Tests for load_expected_data."""

    def test_load_valid_file(self):
        """Valid JSON file loads correctly."""
        data = {"Table": [{"attributes": {"name": "orders"}}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            result = load_expected_data(f.name)

        os.unlink(f.name)
        assert result == data

    def test_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_expected_data("/nonexistent/path.json")

    def test_invalid_json_structure(self):
        """Non-dict JSON raises ValueError."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([1, 2, 3], f)
            f.flush()

        with pytest.raises(ValueError, match="JSON object"):
            load_expected_data(f.name)

        os.unlink(f.name)


class TestLoadActualOutput:
    """Tests for load_actual_output."""

    def test_load_jsonl_files(self):
        """JSONL files in output directory are loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_dir = os.path.join(tmpdir, "wf1", "run1", "table")
            os.makedirs(workflow_dir)

            records = [
                {"typeName": "Table", "attributes": {"name": "orders"}},
                {"typeName": "Table", "attributes": {"name": "users"}},
            ]
            with open(os.path.join(workflow_dir, "table.json"), "wb") as f:
                for r in records:
                    f.write(json.dumps(r).encode() + b"\n")

            result = load_actual_output(tmpdir, "wf1", "run1")
            assert len(result) == 2
            assert result[0]["attributes"]["name"] == "orders"

    def test_directory_not_found(self):
        """Missing directory raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_actual_output("/nonexistent", "wf1", "run1")

    def test_empty_directory(self):
        """Empty directory raises FileNotFoundError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_dir = os.path.join(tmpdir, "wf1", "run1")
            os.makedirs(workflow_dir)

            with pytest.raises(FileNotFoundError, match="No metadata records"):
                load_actual_output(tmpdir, "wf1", "run1")

"""Metadata comparison engine for integration testing.

This module compares actual extracted metadata against an expected baseline
and produces a gap report detailing missing assets, extra assets, and
attribute mismatches.

The comparison is connector-agnostic — it works with any asset type
(Database, Schema, Table, Column, etc.) as long as assets follow the
standard structure with ``typeName``, ``attributes``, and ``customAttributes``.

Example expected data JSON::

    {
        "Database": [
            {"attributes": {"name": "mydb", "connectorName": "postgres"}}
        ],
        "Table": [
            {"attributes": {"name": "orders", "columnCount": 6}}
        ]
    }

Usage::

    from application_sdk.test_utils.integration.comparison import (
        compare_metadata, load_expected_data, load_actual_output,
    )

    expected = load_expected_data("tests/expected/baseline.json")
    actual = load_actual_output("/tmp/output", workflow_id, run_id)
    report = compare_metadata(expected, actual, strict=True)
    if report.has_gaps:
        raise AssertionError(report.format_report())
"""

import os
from dataclasses import dataclass, field
from glob import glob
from typing import Any, Dict, List, Optional, Set

import orjson

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Fields that change between runs and should be ignored by default
DEFAULT_IGNORED_FIELDS: Set[str] = {
    "qualifiedName",
    "connectionQualifiedName",
    "lastSyncWorkflowName",
    "lastSyncRun",
    "lastSyncRunAt",
    "tenantId",
    "connectionName",
    "databaseQualifiedName",
    "schemaQualifiedName",
    "tableQualifiedName",
    "viewQualifiedName",
}

# Nested reference fields that contain run-specific qualified names
DEFAULT_IGNORED_NESTED_FIELDS: Set[str] = {
    "atlanSchema",
    "database",
    "table",
    "view",
    "materialisedView",
    "parentTable",
}


@dataclass
class AssetDiff:
    """A single difference found between expected and actual metadata.

    Attributes:
        asset_type: The typeName of the asset (e.g., "Table", "Column").
        asset_name: The name of the asset from attributes.name.
        diff_type: Category of difference — one of "missing", "extra",
            "attribute_mismatch", "missing_attribute", "count_mismatch".
        field: Dot-separated path to the differing field
            (e.g., "attributes.columnCount"). None for asset-level diffs.
        expected: The expected value. None for extra assets.
        actual: The actual value. None for missing assets.
    """

    asset_type: str
    asset_name: str
    diff_type: str
    field: Optional[str] = None
    expected: Any = None
    actual: Any = None

    def __str__(self) -> str:
        if self.diff_type in ("missing", "extra"):
            return f"[{self.diff_type.upper()}] {self.asset_type}/{self.asset_name}"
        return (
            f"[{self.diff_type.upper()}] {self.asset_type}/{self.asset_name} "
            f"-> {self.field}: expected={self.expected!r}, actual={self.actual!r}"
        )


@dataclass
class GapReport:
    """Summary of all differences between expected and actual metadata.

    Attributes:
        diffs: List of individual asset differences.
        summary: Count of diffs by type.
    """

    diffs: List[AssetDiff] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)

    @property
    def has_gaps(self) -> bool:
        """Return True if any differences were found."""
        return len(self.diffs) > 0

    def format_report(self) -> str:
        """Format the gap report as a human-readable string for pytest output."""
        if not self.has_gaps:
            return "No gaps found — actual metadata matches expected."

        lines = ["Metadata validation failed:", ""]

        # Summary
        lines.append("Summary:")
        for diff_type, count in sorted(self.summary.items()):
            lines.append(f"  {diff_type}: {count}")
        lines.append("")

        # Group diffs by asset type
        by_type: Dict[str, List[AssetDiff]] = {}
        for diff in self.diffs:
            by_type.setdefault(diff.asset_type, []).append(diff)

        for asset_type, type_diffs in sorted(by_type.items()):
            lines.append(f"[{asset_type}]")
            for diff in type_diffs:
                lines.append(f"  {diff}")
            lines.append("")

        return "\n".join(lines)


def compare_metadata(
    expected: Dict[str, List[Dict[str, Any]]],
    actual: List[Dict[str, Any]],
    strict: bool = True,
    ignored_fields: Optional[Set[str]] = None,
) -> GapReport:
    """Compare actual extracted metadata against an expected baseline.

    Args:
        expected: Expected metadata grouped by asset type. Keys are type names
            (e.g., "Table"), values are lists of asset dicts with ``attributes``
            and optionally ``customAttributes``.
        actual: List of actual extracted asset records, each with ``typeName``,
            ``attributes``, and optionally ``customAttributes``.
        strict: If True, extra assets in actual output that are not in the
            expected data will be reported as gaps.
        ignored_fields: Set of attribute field names to skip during comparison.
            Defaults to ``DEFAULT_IGNORED_FIELDS``.

    Returns:
        GapReport: A report of all differences found.
    """
    if ignored_fields is None:
        ignored_fields = DEFAULT_IGNORED_FIELDS

    report = GapReport()

    # Group actual assets by typeName
    actual_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for record in actual:
        type_name = record.get("typeName", "Unknown")
        actual_by_type.setdefault(type_name, []).append(record)

    for asset_type, expected_assets in expected.items():
        actual_assets = actual_by_type.get(asset_type, [])

        # Count check
        if len(expected_assets) != len(actual_assets):
            report.diffs.append(
                AssetDiff(
                    asset_type=asset_type,
                    asset_name="*",
                    diff_type="count_mismatch",
                    field="count",
                    expected=len(expected_assets),
                    actual=len(actual_assets),
                )
            )
            _increment_summary(report, "count_mismatch")

        # Build lookup dict for actual assets keyed by attributes.name
        actual_by_name: Dict[str, Dict[str, Any]] = {}
        for asset in actual_assets:
            name = _get_asset_name(asset)
            if name:
                actual_by_name[name] = asset

        # Check each expected asset
        expected_names: Set[str] = set()
        for expected_asset in expected_assets:
            name = _get_asset_name(expected_asset)
            if not name:
                logger.warning(
                    f"Expected {asset_type} asset has no attributes.name, skipping"
                )
                continue

            expected_names.add(name)
            actual_asset = actual_by_name.get(name)

            if actual_asset is None:
                report.diffs.append(
                    AssetDiff(
                        asset_type=asset_type,
                        asset_name=name,
                        diff_type="missing",
                    )
                )
                _increment_summary(report, "missing")
                continue

            # Compare attributes
            _compare_attributes(
                report=report,
                asset_type=asset_type,
                asset_name=name,
                expected_attrs=expected_asset.get("attributes", {}),
                actual_attrs=actual_asset.get("attributes", {}),
                prefix="attributes",
                ignored_fields=ignored_fields,
            )

            # Compare customAttributes
            expected_custom = expected_asset.get("customAttributes")
            actual_custom = actual_asset.get("customAttributes")
            if expected_custom:
                _compare_attributes(
                    report=report,
                    asset_type=asset_type,
                    asset_name=name,
                    expected_attrs=expected_custom,
                    actual_attrs=actual_custom or {},
                    prefix="customAttributes",
                    ignored_fields=ignored_fields,
                )

        # Check for extra assets in actual (strict mode)
        if strict:
            for name in actual_by_name:
                if name not in expected_names:
                    report.diffs.append(
                        AssetDiff(
                            asset_type=asset_type,
                            asset_name=name,
                            diff_type="extra",
                        )
                    )
                    _increment_summary(report, "extra")

    # Check for asset types in actual that aren't in expected (strict mode)
    if strict:
        for asset_type in actual_by_type:
            if asset_type not in expected:
                for asset in actual_by_type[asset_type]:
                    name = _get_asset_name(asset) or "<unnamed>"
                    report.diffs.append(
                        AssetDiff(
                            asset_type=asset_type,
                            asset_name=name,
                            diff_type="extra",
                        )
                    )
                    _increment_summary(report, "extra")

    return report


def load_expected_data(file_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Load expected metadata from a JSON file.

    The file should contain a JSON object mapping asset type names to lists
    of asset records.

    Args:
        file_path: Path to the expected data JSON file.

    Returns:
        Dict mapping asset type names to lists of asset dicts.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file content is not valid JSON or wrong structure.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Expected data file not found: {file_path}")

    with open(file_path, "rb") as f:
        data = orjson.loads(f.read())

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected data file must contain a JSON object mapping asset types "
            f"to lists, got {type(data).__name__}"
        )

    for key, value in data.items():
        if not isinstance(value, list):
            raise ValueError(
                f"Expected data for asset type '{key}' must be a list, "
                f"got {type(value).__name__}"
            )

    return data


def load_actual_output(
    base_path: str, workflow_id: str, run_id: str
) -> List[Dict[str, Any]]:
    """Load all extracted metadata from the output directory.

    Reads JSONL files from ``{base_path}/{workflow_id}/{run_id}/`` and returns
    all records as a flat list.

    Args:
        base_path: Base directory where connector writes extracted output.
        workflow_id: The workflow ID from the API response.
        run_id: The run ID from the API response.

    Returns:
        List of asset records (each with typeName, attributes, etc.).

    Raises:
        FileNotFoundError: If the output directory does not exist or is empty.
    """
    output_dir = os.path.join(base_path, workflow_id, run_id)

    if not os.path.isdir(output_dir):
        raise FileNotFoundError(f"Extracted output directory not found: {output_dir}")

    records: List[Dict[str, Any]] = []
    json_files = glob(os.path.join(output_dir, "**", "*.json"), recursive=True)

    for json_file in sorted(json_files):
        with open(json_file, "rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(orjson.loads(line))

    if not records:
        raise FileNotFoundError(
            f"No metadata records found in output directory: {output_dir}"
        )

    logger.info(f"Loaded {len(records)} actual metadata records from {output_dir}")
    return records


def _get_asset_name(asset: Dict[str, Any]) -> Optional[str]:
    """Extract the name from an asset's attributes."""
    attrs = asset.get("attributes", {})
    if isinstance(attrs, dict):
        return attrs.get("name")
    return None


def _compare_attributes(
    report: GapReport,
    asset_type: str,
    asset_name: str,
    expected_attrs: Dict[str, Any],
    actual_attrs: Dict[str, Any],
    prefix: str,
    ignored_fields: Set[str],
) -> None:
    """Compare attributes between expected and actual, adding diffs to report."""
    if not expected_attrs:
        return

    for key, expected_value in expected_attrs.items():
        if key in ignored_fields:
            continue

        if key in DEFAULT_IGNORED_NESTED_FIELDS:
            continue

        field_path = f"{prefix}.{key}"

        if key not in actual_attrs:
            report.diffs.append(
                AssetDiff(
                    asset_type=asset_type,
                    asset_name=asset_name,
                    diff_type="missing_attribute",
                    field=field_path,
                    expected=expected_value,
                    actual=None,
                )
            )
            _increment_summary(report, "missing_attribute")
            continue

        actual_value = actual_attrs[key]

        if expected_value != actual_value:
            report.diffs.append(
                AssetDiff(
                    asset_type=asset_type,
                    asset_name=asset_name,
                    diff_type="attribute_mismatch",
                    field=field_path,
                    expected=expected_value,
                    actual=actual_value,
                )
            )
            _increment_summary(report, "attribute_mismatch")


def _increment_summary(report: GapReport, diff_type: str) -> None:
    """Increment the count for a diff type in the report summary."""
    report.summary[diff_type] = report.summary.get(diff_type, 0) + 1

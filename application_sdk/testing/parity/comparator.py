"""Core comparison engine for parity testing.

Loads NDJSON output from two extraction runs, strips volatile fields,
joins on qualifiedName, and classifies each asset as ADDED, REMOVED,
MODIFIED, or UNCHANGED.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Set

from application_sdk.testing.parity.models import (
    AssetDiff,
    CategoryResult,
    FieldDiff,
)

# Fields that change every run and must be ignored in comparison.
VOLATILE_FIELDS: Set[str] = {
    "lastSyncWorkflowName",
    "lastSyncRun",
    "lastSyncRunAt",
}


def load_ndjson(directory: Path) -> List[Dict[str, Any]]:
    """Load all NDJSON files from a directory, skipping statistics files."""
    assets: List[Dict[str, Any]] = []
    if not directory.exists():
        return assets
    for json_file in sorted(directory.glob("*.json")):
        if "statistics" in json_file.name:
            continue
        with open(json_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    assets.append(json.loads(line))
    return assets


def strip_volatile(obj: Any) -> Any:
    """Recursively remove volatile fields from a dict."""
    if not isinstance(obj, dict):
        return obj
    return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_FIELDS}


def get_qualified_name(asset: Dict[str, Any]) -> str:
    """Extract qualifiedName from an asset."""
    return asset.get("attributes", {}).get("qualifiedName", "")


def diff_dicts(
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    prefix: str = "",
) -> List[FieldDiff]:
    """Deep diff two dicts, returning a list of field differences."""
    diffs: List[FieldDiff] = []
    all_keys = set(baseline.keys()) | set(candidate.keys())

    for key in sorted(all_keys):
        path = f"{prefix}.{key}" if prefix else key
        b_val = baseline.get(key)
        c_val = candidate.get(key)

        if b_val == c_val:
            continue

        if isinstance(b_val, dict) and isinstance(c_val, dict):
            diffs.extend(diff_dicts(b_val, c_val, path))
        else:
            diffs.append(
                FieldDiff(
                    field_path=path,
                    baseline_value=b_val,
                    candidate_value=c_val,
                )
            )

    return diffs


def compare_category(
    category: str,
    baseline_dir: Path,
    candidate_dir: Path,
) -> CategoryResult:
    """Compare a single category (e.g., 'table', 'column') between two runs."""
    baseline_assets = load_ndjson(baseline_dir)
    candidate_assets = load_ndjson(candidate_dir)

    baseline_map: Dict[str, Dict[str, Any]] = {}
    for asset in baseline_assets:
        qn = get_qualified_name(asset)
        if qn:
            baseline_map[qn] = strip_volatile(asset)

    candidate_map: Dict[str, Dict[str, Any]] = {}
    for asset in candidate_assets:
        qn = get_qualified_name(asset)
        if qn:
            candidate_map[qn] = strip_volatile(asset)

    result = CategoryResult(
        category=category,
        baseline_count=len(baseline_map),
        candidate_count=len(candidate_map),
    )

    # REMOVED: in baseline but not in candidate
    for qn in sorted(set(baseline_map) - set(candidate_map)):
        asset = baseline_map[qn]
        result.removed.append(
            AssetDiff(
                qualified_name=qn,
                type_name=asset.get("typeName", "?"),
                diff_type="REMOVED",
            )
        )

    # ADDED: in candidate but not in baseline
    for qn in sorted(set(candidate_map) - set(baseline_map)):
        asset = candidate_map[qn]
        result.added.append(
            AssetDiff(
                qualified_name=qn,
                type_name=asset.get("typeName", "?"),
                diff_type="ADDED",
            )
        )

    # MODIFIED: in both, check for field diffs
    for qn in sorted(set(baseline_map) & set(candidate_map)):
        field_diffs = diff_dicts(baseline_map[qn], candidate_map[qn])
        if field_diffs:
            result.modified.append(
                AssetDiff(
                    qualified_name=qn,
                    type_name=baseline_map[qn].get("typeName", "?"),
                    diff_type="MODIFIED",
                    field_diffs=field_diffs,
                )
            )

    return result


def discover_categories(baseline_dir: Path, candidate_dir: Path) -> List[str]:
    """Find all category subdirectories across both dirs."""
    categories: Set[str] = set()
    for d in [baseline_dir, candidate_dir]:
        if d.exists():
            for subdir in d.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    categories.add(subdir.name)
    return sorted(categories)


def run_comparison(
    baseline_dir: Path,
    candidate_dir: Path,
) -> List[CategoryResult]:
    """Run full parity comparison across all categories."""
    categories = discover_categories(baseline_dir, candidate_dir)
    return [
        compare_category(cat, baseline_dir / cat, candidate_dir / cat)
        for cat in categories
    ]

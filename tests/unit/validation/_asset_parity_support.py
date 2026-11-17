"""Synthetic batches and the full output projection for the asset-validation parity test.

``test_asset_report_bounds.py`` compares what the walk produces for these batches with
``resources/asset_validation_parity_golden.json``. The golden file was written by
running :func:`project_all` against the unbounded walk — every failure and orphan
held in the report — so a difference means a surface a consumer reads has changed.

Everything here uses only names the unbounded walk also has, so the golden can be
regenerated against a checkout of it. Run this file as a script from this checkout,
so the module comes from here and ``application_sdk`` from the other::

    PYTHONPATH=<unbounded checkout> python tests/unit/validation/_asset_parity_support.py OUT.json

Paths are relative to the batch root (the caller ``chdir``s there), so the file names
inside the matrix and the WARNING body are stable across machines.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

CONN = "default/snow/123"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"


def _table(name: str) -> dict[str, Any]:
    qn = f"{SCHEMA_QN}/{name}"
    return {
        "typeName": "Table",
        "attributes": {
            "name": name,
            "connectorName": "snow",
            "connectionQualifiedName": CONN,
            "databaseName": "DB",
            "databaseQualifiedName": f"{CONN}/DB",
            "schemaName": "SCHEMA",
            "schemaQualifiedName": SCHEMA_QN,
            "qualifiedName": qn,
        },
        "relationshipAttributes": {
            "atlanSchema": {
                "typeName": "Schema",
                "qualifiedName": SCHEMA_QN,
                "uniqueAttributes": {"qualifiedName": SCHEMA_QN},
            }
        },
    }


def _column(name: str, table: str, *, valid: bool, qn: str | None = None) -> dict:
    table_qn = f"{SCHEMA_QN}/{table}"
    attributes: dict[str, Any] = {
        "name": name,
        "connectorName": "snow",
        "connectionQualifiedName": CONN,
        "databaseName": "DB",
        "databaseQualifiedName": f"{CONN}/DB",
        "schemaName": "SCHEMA",
        "schemaQualifiedName": SCHEMA_QN,
        "tableQualifiedName": table_qn,
        "qualifiedName": qn or f"{table_qn}/{name}",
    }
    if valid:
        attributes["order"] = 1
        attributes["tableName"] = table
    return {
        "typeName": "Column",
        "attributes": attributes,
        "relationshipAttributes": {
            "table": {
                "typeName": "Table",
                "qualifiedName": table_qn,
                "uniqueAttributes": {"qualifiedName": table_qn},
            }
        },
    }


def _lines(records: list[Any]) -> bytes:
    out = []
    for record in records:
        out.append(record if isinstance(record, bytes) else json.dumps(record).encode())
    return b"\n".join(out) + b"\n"


def _put(root: Path, rel: str, records: list[Any]) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_lines(records))


def write_overflow_batch(root: Path) -> None:
    """More failures and more orphans than the per-axis cap, of every kind.

    Three present tables; 40 invalid columns on them, interleaved with 8
    undeserializable lines of three decode classes and one connection-prefix miss
    (49 failures); 30 valid columns on 12 absent tables, some referenced several
    times, plus the never-emitted schema (13 orphans) — and 20 more absent tables
    to push the orphan axis past the cap too.
    """
    _put(root, "transformed/Table/chunk-0.json", [_table(f"T{i}") for i in range(3)])
    mixed: list[Any] = []
    for i in range(40):
        mixed.append(_column(f"C{i}", f"T{i % 3}", valid=False))
        if i % 5 == 4:
            kind = (i // 5) % 3
            if kind == 0:
                mixed.append(b'{"typeName":"Column","attributes":')
            elif kind == 1:
                bad = _column(f"BAD{i}", "T0", valid=True)
                bad["attributes"]["order"] = "not-a-number"
                mixed.append(bad)
            else:
                mixed.append(b"12345")
    mixed.append(
        _column("PFX", "T0", valid=True, qn="default/other/9/DB/SCHEMA/T0/PFX")
    )
    _put(root, "transformed/Column/chunk-0.json", mixed)
    _put(
        root,
        "transformed/Column/chunk-1.json",
        [_column(f"O{i}", f"MISSING{i % 12}", valid=True) for i in range(30)]
        + [_column(f"Z{i}", f"ZMISSING{i}", valid=True) for i in range(20)],
    )


def write_few_failures_batch(root: Path) -> None:
    """Fewer failures than the cap, more orphans than it.

    The shared report lists failures before orphans, so its first rows are the
    failures and the orphan samples fill the rest — the case where both axes reach
    the same listing.
    """
    _put(
        root,
        "transformed/Column/chunk-0.json",
        [_column(f"C{i}", "T0", valid=False) for i in range(6)]
        + [_column(f"O{i}", f"MISSING{i:02d}", valid=True) for i in range(30)],
    )
    _put(root, "transformed/Table/chunk-0.json", [_table("T0")])


def write_clean_batch(root: Path) -> None:
    """Nothing wrong but the never-emitted schema — one orphan, no failures."""
    _put(root, "transformed/Table/chunk-0.json", [_table(f"T{i}") for i in range(4)])
    _put(
        root,
        "transformed/Column/chunk-0.json",
        [_column(f"C{i}", f"T{i % 4}", valid=True) for i in range(20)],
    )


BATCHES = {
    "overflow": write_overflow_batch,
    "few_failures": write_few_failures_batch,
    "clean": write_clean_batch,
}


def project_all(name: str) -> dict[str, Any]:
    """Every surface a consumer reads, for one batch already written under ``.``.

    The single-path cell (what the wrapper dispatches to) is projected onto the
    shipped event map, the WARNING body at the hook's cap and at a smaller one, and
    the shared report's counts, body, matrix and event map. The multi-part cell
    (``upload_refs``) and the wrapper are projected onto the event map.
    """
    from pyatlan_v9.model.assets import Asset

    from application_sdk.validation import (
        AssetBatchScope,
        ModelSource,
        asset_validation_event_fields,
        validate_artifact,
        validate_assets_as_artifact,
    )
    from application_sdk.validation.artifacts import (
        artifact_validation_event_fields,
        artifact_validation_matrix_json,
    )

    parts = sorted(str(p) for p in Path("transformed").iterdir())
    runs = {
        "single": validate_assets_as_artifact("transformed"),
        "multi_part": validate_assets_as_artifact(parts),
        "wrapper": validate_artifact("transformed", ModelSource(model=Asset)),
    }
    out: dict[str, Any] = {}
    for label, report in runs.items():
        assets = report.assets
        out[label] = {
            "event_fields": asset_validation_event_fields(
                assets,
                app_name="example-app",
                scope=AssetBatchScope(
                    upload_kind="upload" if label != "multi_part" else "upload_refs",
                    parts_validated=1 if label != "multi_part" else len(parts),
                    parts_not_local=0,
                ),
            ),
        }
        if label != "single":
            # The other two reach the same walk; their event map is what differs
            # by route, and the rest would repeat the single-path projection.
            continue
        out[label] |= {
            "assets_body": assets.format_report(),
            "assets_body_5": assets.format_report(max_items=5),
            "assets_ok": assets.ok,
            "assets_failed": assets.failed,
            "shared_total": report.total,
            "shared_passed": report.passed,
            "shared_failed": report.failed,
            "shared_undecodable": report.undecodable,
            "shared_ok": report.ok,
            "shared_outcome": report.outcome,
            "shared_body": report.format_report(),
            "shared_body_5": report.format_report(max_items=5),
            "shared_matrix": artifact_validation_matrix_json(report),
            "shared_event_fields": artifact_validation_event_fields(report),
        }
    return out


def generate(root: Path) -> dict[str, Any]:
    """Write each batch under ``root/<name>`` and project it, ``chdir``-ing in."""
    golden: dict[str, Any] = {}
    cwd = os.getcwd()
    try:
        for name, write in BATCHES.items():
            batch = root / name
            batch.mkdir(parents=True)
            write(batch)
            os.chdir(batch)
            golden[name] = project_all(name)
            os.chdir(cwd)
    finally:
        os.chdir(cwd)
    return golden


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        result = generate(Path(scratch))
    Path(sys.argv[1]).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")

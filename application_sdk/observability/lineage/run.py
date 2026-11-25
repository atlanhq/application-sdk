"""The coverage step: output files plus recorded reasons in, rows and a summary out.

:func:`run_coverage_step` is the one call an app makes after its lineage step. It
never raises. Whatever goes wrong, the app gets a summary whose
``coverageStatus`` says so, and the lineage it already wrote is untouched: this
step only reads the app's output and writes under its own directory.

Statuses:

- ``OK``: rows written from the output and the recorder.
- ``DEGRADED``: the recorder stopped after an error during the lineage step.
  Rows are still written from the output, but reasons recorded after the error
  are missing, so a miss may read as uninstrumented.
- ``FAILED``: the step itself failed; ``coverageError`` names the exception type.
"""

from __future__ import annotations

import contextlib
import os
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

import orjson

from application_sdk.observability.lineage.assessment import (
    Assessment,
    CoverageCounts,
    LineageStatus,
    assess,
)
from application_sdk.observability.lineage.outcome import coverage_outcome_fields
from application_sdk.observability.lineage.reader import (
    CoverageSpec,
    DerivedAsset,
    ReadStats,
    read_run_output,
)
from application_sdk.observability.lineage.recorder import Recorded
from application_sdk.observability.lineage.taxonomy import (
    ReasonRegistry,
    UninstrumentedPath,
    Verdict,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

__all__ = [
    "ASSETS_FILE",
    "LEDGER_VERSION",
    "REFS_FILE",
    "SUMMARY_FILE",
    "classify",
    "run_coverage_step",
    "write_coverage",
]

#: Bump when a row's keys or their meaning change.
LEDGER_VERSION = 7

ASSETS_FILE = "lineage-coverage-assets.json"
REFS_FILE = "lineage-coverage-refs.json"
SUMMARY_FILE = "lineage-coverage.json"

Refine = Callable[[DerivedAsset, Recorded | None, Assessment], Assessment | None]


def classify(
    asset: DerivedAsset,
    recorded: Recorded | None,
    spec: CoverageSpec,
    registry: ReasonRegistry,
) -> Assessment:
    """The stage-1 verdict for one asset.

    Order: what the app said can't be trusted (``unusable``), then what the
    output shows, then why nothing was built (``missing``).
    """
    if recorded is not None and recorded.unusable:
        return assess(
            LineageStatus.MISSING_LINEAGE, registry.resolve(recorded.unusable[0])
        )
    partial = (
        registry.resolve(recorded.partial[0])
        if recorded is not None and recorded.partial
        else None
    )
    if asset.processes and asset.refs:
        if not asset.resolvable_refs:
            if asset.ref_connectors & spec.generic_connectors:
                return assess(LineageStatus.MISSING_LINEAGE, spec.generic_reason)
            return Assessment(
                Verdict.VALID_NONE, spec.non_sql_reason, basis="refs_not_sql"
            )
        return assess(LineageStatus.PENDING, partial)
    if asset.has_lineage:
        if partial is not None:
            return assess(LineageStatus.PARTIAL_LINEAGE, partial)
        return assess(LineageStatus.HAS_LINEAGE)
    # The first reason recorded stands: later paths are fallbacks of the first.
    code = recorded.missing[0] if recorded is not None and recorded.missing else None
    return assess(
        LineageStatus.MISSING_LINEAGE,
        registry.resolve(code) if code else UninstrumentedPath,
    )


def _row(
    asset: DerivedAsset,
    recorded: Recorded | None,
    assessment: Assessment,
    key: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "assetType": asset.type_name,
        "qualifiedName": asset.qualified_name,
        "name": asset.name,
        "assetKey": key,
        **assessment.to_row(),
        "hasLineage": asset.has_lineage,
        "processCount": asset.processes,
        "processQualifiedName": asset.process_qualified_name,
        "refCount": asset.refs,
        "resolvableRefCount": asset.resolvable_refs,
        "fieldsTotal": asset.fields_total,
        "fieldsWithLineage": asset.fields_with_lineage,
        "fieldsPending": asset.fields_pending,
    }
    if asset.attributes:
        row["entityAttributes"] = asset.attributes
    if recorded is not None:
        # The code string as recorded, so one the registry doesn't know stays visible.
        if assessment.verdict is Verdict.MISSING:
            codes = recorded.unusable or recorded.missing
            if codes and assessment.reason is UninstrumentedPath:
                row["recordedCode"] = codes[0]
        if recorded.missing:
            row["failedAttempts"] = list(recorded.missing)
        if recorded.partial:
            row["partialFailures"] = list(recorded.partial)
        if recorded.unusable:
            row["unusable"] = list(recorded.unusable)
        if recorded.details:
            row["reasonDetails"] = recorded.details
        if recorded.attributes:
            row["attributes"] = recorded.attributes
        if recorded.field_missing:
            row["fieldReasons"] = dict(recorded.field_missing)
    return row


def write_coverage(
    paths: Iterable[str],
    output_dir: str,
    spec: CoverageSpec,
    recorder: Any,
    registry: ReasonRegistry,
    run: dict[str, Any] | None = None,
    refine: Refine | None = None,
) -> dict[str, Any]:
    """Write the asset rows, the ref rows and the summary. Raises on failure;
    apps call :func:`run_coverage_step`, which doesn't."""
    os.makedirs(output_dir, exist_ok=True)
    stats = ReadStats()
    counts = CoverageCounts()
    by_type: dict[str, Counter[str]] = {}
    rows = 0
    assets_path = os.path.join(output_dir, ASSETS_FILE)
    refs_path = os.path.join(output_dir, REFS_FILE)
    with open(refs_path, "wb") as refs_out, open(assets_path, "wb") as assets_out:
        for asset in read_run_output(paths, spec, refs_out, stats):
            key = spec.key_for(asset.type_name, asset.qualified_name)
            recorded = recorder.get(key)
            assessment = classify(asset, recorded, spec, registry)
            if refine is not None:
                assessment = refine(asset, recorded, assessment) or assessment
            counts.add(assessment)
            by_type.setdefault(asset.type_name, Counter())[
                assessment.verdict.value
            ] += 1
            assets_out.write(
                orjson.dumps(_row(asset, recorded, assessment, key), default=str)
            )
            assets_out.write(b"\n")
            rows += 1
    recorder_error = getattr(recorder, "error", None)
    summary: dict[str, Any] = {
        "appName": spec.app_name,
        "ledgerVersion": LEDGER_VERSION,
        "coverageStatus": "DEGRADED" if recorder_error else "OK",
        "recorderError": recorder_error,
        "run": run or {},
        "totals": {
            "assets": rows,
            "refs": stats.refs,
            "coverageRate": counts.coverage_rate,
        },
        "verdicts": {v.value: n for v, n in sorted(counts.verdicts.items())},
        "byType": {t: dict(sorted(c.items())) for t, c in sorted(by_type.items())},
        "gaps": {g.value: n for g, n in sorted(counts.gaps.items())},
        "categories": dict(sorted(counts.categories.items())),
        "read": {
            "files": stats.files,
            "entities": stats.entities,
            "processes": stats.processes,
            "columnProcesses": stats.column_processes,
            "unreadableLines": stats.unreadable_lines,
            "processOutputsWithoutAnAsset": stats.process_outputs_without_an_asset,
        },
        "outcome": coverage_outcome_fields(counts, spec.app_name),
    }
    with open(os.path.join(output_dir, SUMMARY_FILE), "wb") as fh:
        fh.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2, default=str))
    return summary


def run_coverage_step(
    paths: Iterable[str],
    output_dir: str,
    spec: CoverageSpec,
    recorder: Any,
    registry: ReasonRegistry,
    run: dict[str, Any] | None = None,
    refine: Refine | None = None,
) -> dict[str, Any]:
    """Run coverage after the lineage step. Never raises.

    Returns the summary; on failure, a summary with ``coverageStatus: FAILED``
    (also written to ``output_dir`` when that is still possible).
    """
    try:
        return write_coverage(paths, output_dir, spec, recorder, registry, run, refine)
    except Exception as exc:  # noqa: BLE001 - coverage must not fail the run
        failed = {
            "appName": getattr(spec, "app_name", None),
            "ledgerVersion": LEDGER_VERSION,
            "coverageStatus": "FAILED",
            "coverageError": type(exc).__name__,
            "run": run or {},
        }
        with contextlib.suppress(Exception):
            logger.warning(
                "Lineage coverage step failed; lineage is unaffected",
                error_type=type(exc).__name__,
            )
            os.makedirs(output_dir, exist_ok=True)
            for name in (ASSETS_FILE, REFS_FILE):
                partial = os.path.join(output_dir, name)
                if os.path.exists(partial):
                    os.remove(partial)
            with open(os.path.join(output_dir, SUMMARY_FILE), "wb") as fh:
                fh.write(orjson.dumps(failed, option=orjson.OPT_INDENT_2))
        return failed

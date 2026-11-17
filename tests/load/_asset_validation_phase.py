"""One phase of the upload-time asset-validation benchmark, run in its own process.

Driven by ``test_asset_validation_budget_load.py``. Each phase runs in a fresh
interpreter because peak RSS (``ru_maxrss``) only ever rises: the scan's child and
the activity process that unpickles its report are different processes in
production, and measured in one they would be charged for each other.

Usage::

    python _asset_validation_phase.py generate ROOT RECORDS PARENTS
    python _asset_validation_phase.py scan ROOT PICKLE_OUT
    python _asset_validation_phase.py project PICKLE_IN

``generate`` writes ``RECORDS`` Columns that each fail ``.validate()`` and each
point at one of ``PARENTS`` Tables the batch never emits — every record a failure,
``PARENTS`` distinct orphans. ``scan`` runs the cell the validation child runs and
pickles its report as the process pool does; ``project`` unpickles it and builds
the event and the WARNING body, as the activity does. Each prints one JSON line.
"""

from __future__ import annotations

import json
import pickle
import resource
import sys
import time
from pathlib import Path

_CONN = "default/snow/123"
_SCHEMA_QN = f"{_CONN}/DB/S"
_COLUMN = (
    '{"attributes":{"name":"C%d","connectorName":"snow",'
    '"connectionQualifiedName":"' + _CONN + '","databaseName":"DB",'
    '"databaseQualifiedName":"' + _CONN + '/DB","schemaName":"S",'
    '"schemaQualifiedName":"' + _SCHEMA_QN + '","tableQualifiedName":"%s",'
    '"qualifiedName":"%s/C%d"},"relationshipAttributes":{"table":'
    '{"qualifiedName":"%s","typeName":"Table","uniqueAttributes":'
    '{"qualifiedName":"%s"}}},"typeName":"Column"}\n'
)
_FILES = 8


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _generate(root: Path, records: int, parents: int) -> dict[str, int]:
    out = root / "transformed" / "Column"
    out.mkdir(parents=True)
    per_file = -(-records // _FILES)
    for f in range(_FILES):
        with open(out / f"chunk-{f}.json", "w") as handle:  # noqa: ASYNC230 — benchmark harness, no event loop
            for i in range(f * per_file, min(records, (f + 1) * per_file)):
                table = f"{_SCHEMA_QN}/T{i % parents}"
                handle.write(_COLUMN % (i, table, table, i, table, table))
    return {"records": records, "parents": parents}


def _scan(root: Path, pickle_out: Path) -> dict[str, int | float | str]:
    from application_sdk.validation.assets import validate_assets_as_artifact

    before = _peak_rss_bytes()
    started = time.monotonic()
    report = validate_assets_as_artifact(str(root / "transformed"))
    blob = pickle.dumps(report, protocol=pickle.HIGHEST_PROTOCOL)
    seconds = time.monotonic() - started
    pickle_out.write_bytes(blob)
    assets = report.assets
    return {
        "baseline_bytes": before,
        "peak_bytes": _peak_rss_bytes(),
        "pickle_bytes": len(blob),
        "seconds": round(seconds, 2),
        "total": assets.total,
        "failed": assets.failed,
        "orphaned": assets.orphaned,
        "truncated": assets.truncated,
    }


def _project(pickle_in: Path) -> dict[str, int]:
    from application_sdk.validation.assets import (
        AssetBatchScope,
        asset_validation_event_fields,
    )

    before = _peak_rss_bytes()
    report = pickle.loads(pickle_in.read_bytes())  # noqa: S301 — this benchmark's own pickle
    fields = asset_validation_event_fields(
        report.assets,
        app_name="example-app",
        scope=AssetBatchScope(
            upload_kind="upload", parts_validated=1, parts_not_local=0
        ),
    )
    report.assets.format_report()
    return {
        "growth_bytes": _peak_rss_bytes() - before,
        "assets_invalid": int(fields["assets_invalid"]),
        "assets_orphaned": int(fields["assets_orphaned"]),
    }


def main() -> None:
    phase = sys.argv[1]
    if phase == "generate":
        result: dict = _generate(Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    elif phase == "scan":
        result = _scan(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        result = _project(Path(sys.argv[2]))
    print(json.dumps(result))  # noqa: T201 — the parent test reads this line


if __name__ == "__main__":
    main()

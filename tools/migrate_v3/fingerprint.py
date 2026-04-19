"""F3: Connector type classification by scanning for v2 / v3 class hierarchy patterns.

Scans Python files (excluding tests) in a directory for characteristic import
and inheritance patterns to determine the connector type without executing code.

Usage::

    from tools.migrate_v3.fingerprint import fingerprint_connector
    result = fingerprint_connector(Path("../my-connector/src"))
    print(result.connector_type)  # "sql_metadata" | "incremental_sql" | "sql_query" | "custom"
    print(result.confidence)      # 0.0–1.0
    print(result.evidence)        # list of human-readable evidence strings
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tools.migrate_v3.check_migration import _is_test_path

# ---------------------------------------------------------------------------
# Detection patterns (priority order matches the detection rules)
# ---------------------------------------------------------------------------

# Priority 1 — must test before sql_metadata patterns because incremental
# connectors ALSO inherit from SQL metadata classes.
_RE_INCREMENTAL = re.compile(r"\bIncrementalSQLMetadataExtractionWorkflow\b")

# Priority 2
_RE_SQL_METADATA_V2 = re.compile(
    r"\b(?:BaseSQLMetadataExtractionWorkflow|BaseSQLMetadataExtractionActivities)\b"
)

# Priority 3
_RE_SQL_QUERY_V2 = re.compile(
    r"\b(?:SQLQueryExtractionWorkflow|SQLQueryExtractionActivities)\b"
)

# Priority 4 — application-level class (lower confidence)
_RE_SQL_METADATA_APP = re.compile(r"\bBaseSQLMetadataExtractionApplication\b")

# Priority 5 — already-migrated v3 patterns
_RE_V3_INCREMENTAL = re.compile(r"\bIncrementalSqlMetadataExtractor\b")
_RE_V3_SQL_METADATA = re.compile(r"\bSqlMetadataExtractor\b")
_RE_V3_SQL_QUERY = re.compile(r"\bSqlQueryExtractor\b")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class FingerprintResult:
    connector_type: str  # "sql_metadata" | "incremental_sql" | "sql_query" | "custom"
    confidence: float  # 0.0–1.0
    evidence: list[str] = field(default_factory=list)
    already_migrated: bool = False


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def fingerprint_connector(root: Path) -> FingerprintResult:
    """Scan Python files under *root* to detect connector type.

    Test files are excluded.  Returns a :class:`FingerprintResult` indicating
    the most likely connector type.
    """
    py_files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
    scan_root = root if root.is_dir() else root.parent

    evidence: list[str] = []
    has_incremental = False
    has_sql_metadata_v2 = False
    has_sql_query_v2 = False
    has_sql_metadata_app = False

    already_migrated_type: str | None = None
    already_migrated_evidence: list[str] = []

    for path in py_files:
        if _is_test_path(path, root=scan_root):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            rel = str(path.relative_to(scan_root))
        except ValueError:
            rel = path.name

        # Priority 1
        if _RE_INCREMENTAL.search(text):
            has_incremental = True
            evidence.append(
                f"{rel}: references IncrementalSQLMetadataExtractionWorkflow"
            )

        # Priority 2
        if _RE_SQL_METADATA_V2.search(text):
            has_sql_metadata_v2 = True
            evidence.append(
                f"{rel}: references BaseSQLMetadataExtractionWorkflow/Activities"
            )

        # Priority 3
        if _RE_SQL_QUERY_V2.search(text):
            has_sql_query_v2 = True
            evidence.append(f"{rel}: references SQLQueryExtractionWorkflow/Activities")

        # Priority 4
        if _RE_SQL_METADATA_APP.search(text):
            has_sql_metadata_app = True
            evidence.append(f"{rel}: references BaseSQLMetadataExtractionApplication")

        # Priority 5 — already migrated
        if _RE_V3_INCREMENTAL.search(text) and already_migrated_type is None:
            already_migrated_type = "incremental_sql"
            already_migrated_evidence.append(
                f"{rel}: already uses IncrementalSqlMetadataExtractor (v3)"
            )
        elif _RE_V3_SQL_METADATA.search(text) and already_migrated_type is None:
            already_migrated_type = "sql_metadata"
            already_migrated_evidence.append(
                f"{rel}: already uses SqlMetadataExtractor (v3)"
            )
        elif _RE_V3_SQL_QUERY.search(text) and already_migrated_type is None:
            already_migrated_type = "sql_query"
            already_migrated_evidence.append(
                f"{rel}: already uses SqlQueryExtractor (v3)"
            )

    # Apply priority rules (highest-confidence first).
    if has_incremental:
        return FingerprintResult(
            connector_type="incremental_sql",
            confidence=1.0,
            evidence=evidence,
        )
    if has_sql_metadata_v2:
        return FingerprintResult(
            connector_type="sql_metadata",
            confidence=1.0,
            evidence=evidence,
        )
    if has_sql_query_v2:
        return FingerprintResult(
            connector_type="sql_query",
            confidence=1.0,
            evidence=evidence,
        )
    if has_sql_metadata_app:
        return FingerprintResult(
            connector_type="sql_metadata",
            confidence=0.9,
            evidence=evidence,
        )
    if already_migrated_type is not None:
        return FingerprintResult(
            connector_type=already_migrated_type,
            confidence=1.0,
            evidence=already_migrated_evidence,
            already_migrated=True,
        )

    return FingerprintResult(
        connector_type="custom",
        confidence=0.5,
        evidence=["No recognizable v2 or v3 base class found"],
    )

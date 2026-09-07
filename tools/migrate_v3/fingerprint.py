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

import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from application_sdk.observability import get_logger
from tools.migrate_v3.check_migration import _is_test_path

logger = get_logger(__name__)


def _blank_tokens(text: str, token_types: frozenset[int]) -> tuple[str, bool]:
    """Blank out tokens of the given type(s) so regexes only match real code.

    Uses the tokenizer (not naive character splitting) so e.g. a ``#`` inside
    a string literal, or a class name mentioned inside a string, is handled
    precisely rather than by guesswork. Returns ``(text, True)`` on success.
    On a file that doesn't tokenize cleanly (e.g. a syntax error introduced
    mid-migration), returns ``(text, False)`` with the ORIGINAL, unblanked
    text — callers of the higher-risk "already migrated" check must treat
    that as "could not verify" and skip the file rather than scan raw text,
    or an untokenizable file with a stray TODO comment / docstring mentioning
    a v3 class name would silently reproduce the false positive this exists
    to prevent.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as e:
        logger.debug("Token-blanking fell back to raw text: %s", e, exc_info=True)
        return text, False

    # Map (row, col) -> absolute string index via each line's starting
    # offset, then blank by absolute character range. This handles a
    # single-line or multiline token identically, with no special-casing:
    # a newline character inside a blanked span is left untouched either
    # way, so line count (and re.MULTILINE's ``^`` anchors) never shifts.
    lines = text.splitlines(keepends=True)
    line_start = [0] * (len(lines) + 1)
    for i, line in enumerate(lines):
        line_start[i + 1] = line_start[i] + len(line)

    out = list(text)
    for tok in tokens:
        if tok.type not in token_types:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        start = line_start[srow - 1] + scol
        end = line_start[erow - 1] + ecol
        for i in range(start, end):
            if out[i] not in ("\n", "\r"):
                out[i] = " "
    return "".join(out), True


def _strip_comments(text: str) -> tuple[str, bool]:
    """Blank ``#`` comments only. Used for the priority 1-4 checks, where a
    false match on stray v2-pattern prose is safe-by-default (it only makes
    the tool treat an already-migrated file as needing more work, never the
    reverse)."""
    return _blank_tokens(text, frozenset({tokenize.COMMENT}))


def _strip_comments_and_strings(text: str) -> tuple[str, bool]:
    """Blank ``#`` comments AND string/docstring literals. Used only for the
    priority 5 "already migrated" check: a real base class in
    ``class X(Base):`` can never legitimately be a string literal, so
    blanking string content cannot hide a genuine v3 subclass declaration —
    but it does close the case a bare comment-strip misses, where a
    docstring quotes an actual code example (e.g. ``class X(SqlMetadataExtractor):
    pass``) as documentation. That is exactly the shape this toolchain's own
    upgrade-guide-style snippets take, so it is not a hypothetical input."""
    return _blank_tokens(text, frozenset({tokenize.COMMENT, tokenize.STRING}))


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


# Priority 5 — already-migrated v3 patterns.
#
# Unlike priorities 1-4 (which only ever appear in real import/inheritance
# code in practice), a bare v3 class name is exactly the vocabulary this
# toolchain's own generated prose uses — rewrite_imports.py's TODO comments
# name v3 classes as suggestions, and MIGRATION_PROMPT.md-derived docstrings
# can too. Comment-stripping (see _strip_comments) closes the `#`-comment
# form of that, but a plain string literal or docstring tokenizes as STRING,
# not COMMENT, so a bare substring search is still foolable by prose living
# in a docstring. Requiring the class name to appear either (a) in a real
# class-inheritance clause or (b) in a real import statement — the same
# convention check_migration.py's _RE_APP_SUBCLASS already uses for its own
# subclass check — means the match can only fire on code a human or codemod
# actually wrote to use the class, not on documentation that merely mentions
# it. re.DOTALL lets \s*/.*  span a multiline class signature or import.
# Two separately-flagged patterns per class, checked via _has_real_reference
# below — NOT one combined pattern. A single regex spanning both forms under
# re.DOTALL lets the import alternative's ".+" swallow newlines too, so a
# harmless "import os" earlier in the file plus an unrelated docstring
# mentioning the class name much later would satisfy the whole alternation
# in one greedy-then-backtrack match (verified empirically). Splitting keeps
# DOTALL scoped to the multiline class-signature case only; the import-line
# case stays single-line by omitting DOTALL there.
def _subclass_pattern(class_name: str) -> re.Pattern[str]:
    return re.compile(r"class\s+\w+\s*\([^)]*\b" + class_name + r"\b[^)]*\)", re.DOTALL)


def _import_pattern(class_name: str) -> re.Pattern[str]:
    return re.compile(
        r"^\s*(?:from\s+\S+\s+import\s+.*|import\s+.*)\b" + class_name + r"\b",
        re.MULTILINE,
    )


def _has_real_reference(
    text: str, subclass_re: re.Pattern[str], import_re: re.Pattern[str]
) -> bool:
    """True if *class_name* appears in actual inheritance or import syntax.

    Deliberately NOT a bare substring/word-boundary search — see the module
    note above priority 5 for why prose (a TODO comment or a docstring) must
    not count as evidence a connector already extends a v3 base class.
    """
    return bool(subclass_re.search(text) or import_re.search(text))


_RE_V3_INCREMENTAL_SUBCLASS = _subclass_pattern("IncrementalSqlMetadataExtractor")
_RE_V3_INCREMENTAL_IMPORT = _import_pattern("IncrementalSqlMetadataExtractor")
_RE_V3_SQL_METADATA_SUBCLASS = _subclass_pattern("SqlMetadataExtractor")
_RE_V3_SQL_METADATA_IMPORT = _import_pattern("SqlMetadataExtractor")
_RE_V3_SQL_QUERY_SUBCLASS = _subclass_pattern("SqlQueryExtractor")
_RE_V3_SQL_QUERY_IMPORT = _import_pattern("SqlQueryExtractor")


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
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Skipping unreadable file %s: %s", path, e, exc_info=True)
            continue

        raw_text = text

        # Comments (including TODOs this same toolchain's rewrite_imports.py
        # inserts, e.g. "# TODO(upgrade-v3): ... SqlMetadataExtractor ...")
        # must not feed the priority 1-4 regexes below. A false match on
        # stray v2-pattern prose here is safe-by-default (see _strip_comments
        # docstring), so priorities 1-4 use this comment-only strip even if
        # it fails to tokenize (falls back to raw_text).
        text, _ = _strip_comments(raw_text)

        # Priority 5 ("already migrated") is the dangerous direction — a
        # false positive here silently skips a connector that still needs
        # migrating. It gets the stricter strip (comments AND strings, so a
        # docstring code example can't pass as a real subclass either) and,
        # per _blank_tokens' contract, is skipped entirely (stripped5_ok
        # gate below) rather than falling back to unblanked raw_text on a
        # tokenize failure.
        text5, stripped5_ok = _strip_comments_and_strings(raw_text)

        try:
            rel = str(path.relative_to(scan_root))
        except ValueError as e:
            logger.debug(
                "Path %s not under scan root %s; using basename: %s",
                path,
                scan_root,
                e,
                exc_info=True,
            )
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

        # Priority 5 — already migrated. Skipped entirely for a file that
        # could not be safely blanked (see stripped5_ok note above).
        if not stripped5_ok:
            continue
        if (
            _has_real_reference(
                text5, _RE_V3_INCREMENTAL_SUBCLASS, _RE_V3_INCREMENTAL_IMPORT
            )
            and already_migrated_type is None
        ):
            already_migrated_type = "incremental_sql"
            already_migrated_evidence.append(
                f"{rel}: already uses IncrementalSqlMetadataExtractor (v3)"
            )
        elif (
            _has_real_reference(
                text5, _RE_V3_SQL_METADATA_SUBCLASS, _RE_V3_SQL_METADATA_IMPORT
            )
            and already_migrated_type is None
        ):
            already_migrated_type = "sql_metadata"
            already_migrated_evidence.append(
                f"{rel}: already uses SqlMetadataExtractor (v3)"
            )
        elif (
            _has_real_reference(
                text5, _RE_V3_SQL_QUERY_SUBCLASS, _RE_V3_SQL_QUERY_IMPORT
            )
            and already_migrated_type is None
        ):
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

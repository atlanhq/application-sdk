"""P040 — unquoted DuckDB reserved keywords in transform SQL templates.

The SDK query transformer (``application_sdk.transformers.query``) compiles an
app's YAML transform templates into a DuckDB ``SELECT``: each flattened column
renders as ``{source_query} AS {name}``
(``QueryBasedTransformer.convert_to_sql_expression``).  A bare DuckDB
**reserved keyword** in the ``source_query:`` value — ``column``, ``order``,
``group``, ``select``, ``qualify``, … — is a *reference* to a source column and
reaches DuckDB unquoted, so every transform of that entity type fails at
runtime with a ``ParserException``.  On SDK >= 3.22 the daft-less runtime
routes ALL transforms through DuckDB, so there is no fallback path: the
template is dead on arrival, yet template loading, imports and mocked unit
tests all pass (observed live on main for a document-store connector in fleet
testing).

YAML-level quoting does not protect the value — ``source_query: "order"`` and
``source_query: order`` parse to the same Python string.  The quotes must be
embedded (``source_query: '"order"'``) so they survive into the generated SQL.

Version scope
-------------
SDK 3.28.0 fixes this at the root: the transformer quotes a ``source_query``
that resolved as a plain column reference, so a reserved keyword renders as
valid SQL with no template change.  This check therefore describes apps pinned
**below** that version (``superseded_by: sdk>=3.28.0`` on the rule) — an older
SDK still fails at runtime, and that population has no other static signal.

The embedded-quote remediation this check's message prescribes is interim
advice for that population only, and it is worse than it looks: below 3.28.0
the transformer matched the quoted text against the available columns *as raw
text*, found nothing, and dropped the attribute from published output — a
silent missing attribute in place of a loud ``ParserException``.  Upgrading is
the real fix; from 3.28.0 both spellings resolve and render identically, so a
template already edited this way keeps working and needs no revert.

Scope: the ``source_query:`` (expression) position only
-------------------------------------------------------
The column **identifier** is deliberately NOT graded: it lands in the ``AS``
alias slot, which DuckDB does not restrict.  Verified against the pinned duckdb
1.5.5 — ``SELECT 1 AS column``, ``AS order``, ``AS select``, ``AS qualify`` and
``AS lambda`` all parse, while a bare *reference* raises
(``SELECT order FROM t`` → ``ParserException``).  ``convert_to_sql_expression``
puts ``column['name']`` only in that alias slot.

Grading the identifier would therefore describe a runtime failure that does not
occur.  (Note it is NOT true that shipped identifiers are always dot-quoted:
``flatten_yaml_columns`` dots a key only when it is nested under a non-leaf
parent, and every shipped template carries ``typeName:`` and ``status:`` as
top-level leaf keys under ``columns:``, emitted bare.  The alias-slot argument
is what makes dropping the check correct, and it stands on its own.)

Discovery
---------
Scans every ``*.yml``/``*.yaml`` outside dot-directories, ``tests/``, and
build/vendor trees, keeping only files that carry BOTH a ``columns:`` key and a
``source_query:`` key — the transform-template shape.  Ordinary CI, Helm, and
compose YAML never declares ``source_query`` and is skipped without parsing.

The scan is line-based (this package deliberately has no YAML dependency —
the same posture as the T016 compose-overlay check).

Inline suppression
------------------
Add ``# conformance: ignore[P040] <reason>`` on the offending line (or the
comment-only line directly above it) — YAML ``#`` comments slot into the shared
directive grammar (see ``_ast_common.parse_toml_suppressions``).

Known limits (intentional — biased toward zero false positives):

* Only simple scalar ``source_query:`` values are inspected; block scalars
  (``|``/``>``) and flow collections are not.  A reserved word inside a
  multi-line CASE expression is not graded.
* Values containing a dot are exempt (the SDK auto-quotes them); embedded
  ``"``-quoted values are exempt (already SQL-quoted); values containing
  whitespace or ``(`` are SQL expressions, not bare column references, and are
  exempt.
* YAML boolean/null scalars (``true``/``false``/``null``/``~``) are literals,
  not identifiers, and are never flagged.
* The ``columns:`` block is tracked by indentation and closes on any line at or
  left of the ``columns:`` key.  A flush-left list (``columns:`` with its
  ``- name:`` items at the same indent) therefore grades nothing; the nested
  mapping form every shipped template uses is tracked correctly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_P040 = "P040"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]

#: DuckDB reserved keywords — the exact ``reserved`` category of
#: ``duckdb_keywords()`` on the pinned duckdb (1.5.5), minus the YAML scalar
#: literals ``true``/``false``/``null``, which parse to non-string values and
#: take the transformer's literal path rather than the identifier path.
#:
#: Generated, not hand-curated:
#:
#:     select keyword_name from duckdb_keywords()
#:     where keyword_category = 'reserved'
#:
#: ``tests/test_transform_templates.py`` asserts this constant still matches the
#: installed DuckDB's reserved set, so the list cannot silently drift when
#: duckdb is bumped.  (A hand-maintained set had drifted already: ``grant`` is
#: *unreserved* — ``SELECT 1 AS grant`` parses fine, a plausible column name on
#: a governance connector — while ``lambda``, ``pivot``, ``pivot_longer``,
#: ``pivot_wider``, ``qualify``, ``summarize`` and ``unpivot`` were missing.)
_DUCKDB_RESERVED_KEYWORDS: frozenset[str] = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "default",
        "deferrable",
        "desc",
        "describe",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "fetch",
        "for",
        "foreign",
        "from",
        "group",
        "having",
        "in",
        "initially",
        "intersect",
        "into",
        "lambda",
        "lateral",
        "leading",
        "limit",
        "not",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "pivot",
        "pivot_longer",
        "pivot_wider",
        "placing",
        "primary",
        "qualify",
        "references",
        "returning",
        "select",
        "show",
        "some",
        "summarize",
        "symmetric",
        "table",
        "then",
        "to",
        "trailing",
        "union",
        "unique",
        "unpivot",
        "using",
        "variadic",
        "when",
        "where",
        "window",
        "with",
    }
)

#: Directory names never containing app transform templates.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "tests",
        "test",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
        "venv",
    }
)

#: The template key whose value reaches the generated SELECT's *expression*
#: slot — the one position where a bare reserved keyword raises.  ``name:`` is
#: deliberately excluded — it reaches only the ``AS`` alias slot, which DuckDB
#: does not restrict (see the module docstring).
_TEMPLATE_VALUE_RE = re.compile(
    r"^\s*(?:-\s+)?(?P<key>source_query)\s*:\s*(?P<value>\S.*?)\s*$"
)

_COLUMNS_KEY_RE = re.compile(r"^\s*columns\s*:", re.MULTILINE)
_SOURCE_QUERY_KEY_RE = re.compile(r"^\s*(?:-\s+)?source_query\s*:", re.MULTILINE)


def _is_transform_template(text: str) -> bool:
    """Whether *text* has the transform-template shape (columns + source_query)."""
    return (
        _COLUMNS_KEY_RE.search(text) is not None
        and _SOURCE_QUERY_KEY_RE.search(text) is not None
    )


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing YAML comment (`` # ...``) outside any quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or value[i - 1].isspace():
                return value[:i].rstrip()
    return value


def _unquote_yaml_scalar(value: str) -> str:
    """Strip ONE layer of matching outer YAML quotes (the YAML syntax layer).

    ``"column"`` → ``column`` (YAML quotes do not survive parsing); but
    ``'"column"'`` → ``"column"`` (the embedded SQL quotes DO survive).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _flagged_identifier(raw_value: str) -> str | None:
    """Return the offending identifier when *raw_value* is a bare reserved
    keyword after YAML unquoting, else ``None``.

    Safe shapes: SQL-quoted (embedded double quotes), dotted (SDK auto-quotes),
    SQL expressions rather than bare column references, non-identifier scalars,
    and anything not in the reserved set.
    """
    value = _unquote_yaml_scalar(_strip_inline_comment(raw_value).strip())
    if not value:
        return None
    if value.startswith('"') and value.endswith('"'):
        return None  # SQL-quoted — survives into the SELECT quoted
    if "." in value:
        return None  # SDK's quote_column_name quotes dotted identifiers
    if any(ch.isspace() for ch in value) or "(" in value:
        return None  # an expression (concat(...), CASE ...), not a bare column
    if value.lower() in _DUCKDB_RESERVED_KEYWORDS:
        return value
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one transform-template *text* for P040 findings.

    Only ``source_query`` entries **inside a ``columns:`` block** are inspected
    (tracked by indentation), so a ``source_query`` under some unrelated
    top-level key is not graded.
    """
    if not _is_transform_template(text):
        return []

    suppressions = parse_toml_suppressions(text)
    findings: list[Finding] = []
    columns_indent: int | None = None  # indent of the open ``columns:`` key
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if columns_indent is not None and _indent(line) <= columns_indent:
                columns_indent = None  # left the columns block
            if columns_indent is None and re.match(r"^\s*columns\s*:\s*$", line):
                columns_indent = _indent(line)
                continue
        if columns_indent is None:
            continue
        m = _TEMPLATE_VALUE_RE.match(line)
        if m is None:
            continue
        identifier = _flagged_identifier(m.group("value"))
        if identifier is None:
            continue
        key = m.group("key")
        findings.append(
            make_toml_finding(
                rule_id=RULE_P040,
                file=file,
                line=lineno,
                column=1,
                message=(
                    f"{file}:{lineno}: transform template {key} '{identifier}' is a "
                    "DuckDB reserved keyword used as a bare column reference. On an "
                    "SDK below 3.28.0 it reaches the generated SELECT unquoted (the "
                    "query transformer renders '{source_query} AS {name}' and only "
                    "auto-quotes dotted identifiers), so every transform of this "
                    "entity type fails at runtime with a DuckDB ParserException — "
                    "latent until the first real pipeline run. On SDK >= 3.28.0 the "
                    "transformer quotes a source_query that resolved as a plain "
                    "column reference, so this template resolves correctly as-is — "
                    "suppress or ignore this finding there (the rule fires until the "
                    "fleet floor crosses 3.28.0; it does not resolve the app's SDK "
                    "version). The fix for the below-3.28.0 population is upgrading "
                    "atlan-application-sdk; only if the app is pinned below that "
                    "version, embed SQL quotes in the value so they survive YAML "
                    f"parsing ({key}: '\"{identifier}\"') — note that on an SDK "
                    "below 3.28.0 the transformer matches that value as raw text, "
                    "resolves nothing, and drops the attribute from published "
                    "output instead of raising."
                ),
                suppressions=suppressions,
            )
        )
    return findings


def discover(root: Path) -> list[Path]:
    """Discover transform-template YAML files under *root*.

    Walks ``*.yml``/``*.yaml`` outside dot-directories, ``tests/``, and
    build/vendor trees, keeping only files with the transform-template shape
    (both a ``columns:`` key and a ``source_query:`` key).
    """
    paths: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        for path in root.rglob(pattern):
            rel_parts = path.relative_to(root).parts[:-1]
            if any(
                part.startswith(".") or part in _EXCLUDED_DIR_NAMES
                for part in rel_parts
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _is_transform_template(text):
                paths.append(path)
    return sorted(paths)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single transform-template file for P040 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_all=lambda paths, root: [f for p in paths for f in scan_path(p, root)],
    description=(
        "P040: lint transform SQL templates for unquoted DuckDB reserved "
        "keywords used as identifiers."
    ),
    discover=discover,
    default_scan_paths=(".",),
)
"""CLI entry point for the transform-template check."""


if __name__ == "__main__":
    sys.exit(main())

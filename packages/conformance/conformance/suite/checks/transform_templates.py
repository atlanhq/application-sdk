"""P040 — unquoted DuckDB reserved keywords in transform SQL templates.

The SDK query transformer (``application_sdk.transformers.query``) compiles an
app's YAML transform templates into a DuckDB ``SELECT``: each entry of the
template's ``columns:`` list renders as ``{source_query} AS {name}``, and the
SDK quotes an identifier only when it contains a dot
(``QueryBasedTransformer.quote_column_name``).  A bare DuckDB **reserved
keyword** used as a template identifier — ``column``, ``order``, ``group``,
``select``, ``table``, ``default``, … — therefore reaches DuckDB unquoted, and
every transform of that entity type fails at runtime with a
``ParserException``.  On SDK >= 3.22 the daft-less runtime routes ALL
transforms through DuckDB, so there is no fallback path: the template is dead
on arrival, yet template loading, imports, and mocked unit tests all pass
(observed live on main for a document-store connector in fleet testing).

YAML-level quoting does not protect the identifier — ``name: "column"`` and
``name: column`` parse to the same Python string.  The quotes must be embedded
in the value (``name: '"column"'``) so they survive into the generated SQL.

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

* Only simple scalar values of ``name`` / ``source_query`` keys are inspected;
  block scalars and flow collections are not.
* Values containing a dot are exempt (the SDK auto-quotes them); embedded
  ``"``-quoted values are exempt (already SQL-quoted).
* YAML boolean/null scalars (``true``/``false``/``null``/``~``) are literals,
  not identifiers, and are never flagged.
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

#: DuckDB reserved keywords (the ``reserved`` category of
#: ``duckdb_keywords()``) that can plausibly appear as metadata field names.
#: Deliberately excludes the YAML scalar literals ``true``/``false``/``null``,
#: which parse to non-string values and take the transformer's literal path,
#: not the identifier path.
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
        "grant",
        "group",
        "having",
        "in",
        "initially",
        "intersect",
        "into",
        "lateral",
        "leading",
        "limit",
        "not",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "placing",
        "primary",
        "references",
        "returning",
        "select",
        "show",
        "some",
        "symmetric",
        "table",
        "then",
        "to",
        "trailing",
        "union",
        "unique",
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

#: The two template keys that render into the generated SELECT.
_TEMPLATE_VALUE_RE = re.compile(
    r"^\s*(?:-\s+)?(?P<key>name|source_query)\s*:\s*(?P<value>\S.*?)\s*$"
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
    non-identifier scalars, and anything not in the reserved set.
    """
    value = _unquote_yaml_scalar(_strip_inline_comment(raw_value).strip())
    if not value:
        return None
    if value.startswith('"') and value.endswith('"'):
        return None  # SQL-quoted — survives into the SELECT quoted
    if "." in value:
        return None  # SDK's quote_column_name quotes dotted identifiers
    if value.lower() in _DUCKDB_RESERVED_KEYWORDS:
        return value
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one transform-template *text* for P040 findings.

    Only ``name`` / ``source_query`` entries **inside a ``columns:`` block**
    are inspected (tracked by indentation), so a template's top-level ``name:``
    key — which never renders into the SELECT — is not graded.
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
                    "DuckDB reserved keyword and reaches the generated SELECT "
                    "unquoted (the SDK query transformer renders '{source_query} "
                    "AS {name}' and only auto-quotes dotted identifiers). On SDK "
                    ">= 3.22 every transform of this entity type fails at runtime "
                    "with a DuckDB ParserException — latent until the first real "
                    "pipeline run. Embed SQL quotes in the value so they survive "
                    f"YAML parsing: {key}: '\"{identifier}\"'."
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
            except OSError:
                continue
            if _is_transform_template(text):
                paths.append(path)
    return sorted(paths)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single transform-template file for P040 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
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

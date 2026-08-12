"""Meta-tests for the P040 transform-template check.

P040 catches the DuckDB reserved-keyword pattern: the SDK query transformer
renders each flattened column as ``{source_query} AS {name}``, so a bare
reserved keyword (``column``, ``order``, ``group``, ...) in the
``source_query:`` **reference** position reaches DuckDB unquoted and every
transform of that entity type fails at runtime with a ParserException on the
daft-less SDK >= 3.22 runtime.  YAML-level quotes do not survive parsing — only
embedded SQL quotes do.

The identifier (alias) position is deliberately not graded — DuckDB accepts a
reserved keyword after ``AS``, so there is no runtime failure to report there.
See the checker's module docstring.

Fixtures use the **nested-mapping shape every shipped template actually uses**
(``columns:`` → ``attributes:`` → ``<identifier>:`` → ``source_query:``), not a
synthetic list-of-dicts: a list is not valid transformer input at all
(``flatten_yaml_columns`` raises ``AttributeError`` on it), so testing against
it would let the rule agree with its tests while disagreeing with production.
``test_p040_silent_on_every_shipped_sdk_template`` pins the real corpus.

Tests cover the fire path, every documented safe shape (embedded SQL quotes,
dotted identifiers, expressions, YAML literals, non-reserved names), scope
containment (only entries inside a ``columns:`` block), discovery gating (only
files with the transform-template shape), and inline suppression.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.transform_templates import (
    _DUCKDB_RESERVED_KEYWORDS,
    discover,
    scan_path,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

#: Directory holding the templates the SDK actually ships.
_SDK_TEMPLATES = (
    Path(__file__).resolve().parents[3]
    / "application_sdk"
    / "transformers"
    / "query"
    / "templates"
)

# The real shape: the identifier is the YAML mapping key, and the value that
# reaches the SELECT's expression slot is the nested `source_query:`.
_TEMPLATE_WITH_RESERVED = """\
entity: COLUMN
columns:
  attributes:
    name:
      source_query: column_name
    someAttr:
      source_query: order
    otherAttr:
      source_query: column
"""

_TEMPLATE_SQL_QUOTED = """\
columns:
  attributes:
    someAttr:
      source_query: '"column"'
    otherAttr:
      source_query: '"order"'
"""

_TEMPLATE_CLEAN = """\
columns:
  attributes:
    qualifiedName:
      source_query: concat(x, '/', y)
    description:
      source_query: remarks
"""

_NOT_A_TEMPLATE = """\
name: order
jobs:
  build:
    steps:
      - name: column
"""


def test_p040_rule_metadata() -> None:
    rule = get_rule("P040")
    assert rule.name == "TransformTemplateReservedKeyword"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.18.0"
    assert rule.category == "transform-templates"


def test_p040_fires_on_bare_reserved_keyword() -> None:
    findings = scan_text(_TEMPLATE_WITH_RESERVED, "app/transformers/column.yaml")
    assert [f.rule_id for f in findings] == ["P040", "P040"]
    # The two offending source_query: reference values.
    assert {f.line for f in findings} == {7, 9}
    assert "ParserException" in findings[0].message
    assert "'\"order\"'" in findings[0].message


def test_p040_silent_on_every_shipped_sdk_template() -> None:
    """The real corpus must stay clean — and must be *gradeable*.

    Guards the failure this rule shipped with: a checker keyed on a shape no
    real template uses passes its own tests while being unable to fire on the
    incident it was written for.
    """
    templates = sorted(_SDK_TEMPLATES.glob("*.yaml"))
    assert templates, f"no SDK templates found under {_SDK_TEMPLATES}"
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert scan_text(text, path.name) == [], f"{path.name} unexpectedly flagged"


def test_p040_fires_on_real_shape_reserved_reference() -> None:
    """A reserved keyword as a source_query in a *real* shipped template.

    Built by mutating an actual template rather than hand-writing the shape, so
    the fixture cannot drift away from what the transformer consumes.
    """
    text = (_SDK_TEMPLATES / "table.yaml").read_text(encoding="utf-8")
    dirty = text.replace("source_query: table_name", "source_query: order", 1)
    assert dirty != text
    findings = scan_text(dirty, "table.yaml")
    assert [f.rule_id for f in findings] == ["P040"]
    assert "order" in findings[0].message


def test_p040_does_not_grade_the_alias_position() -> None:
    """A reserved keyword as the column *identifier* is not a runtime failure.

    DuckDB accepts any keyword after ``AS`` (verified on the pinned 1.5.5), and
    ``convert_to_sql_expression`` puts the identifier only in that alias slot.
    Flagging it would describe a failure that does not occur.
    """
    text = (
        "columns:\n"
        "  attributes:\n"
        "    order:\n"
        "      source_query: safe_column\n"
        "    column:\n"
        "      source_query: another_safe_column\n"
    )
    assert scan_text(text, "t.yaml") == []


def test_p040_keyword_set_matches_pinned_duckdb() -> None:
    """The constant must not drift from the DuckDB it claims to describe.

    Skipped when duckdb is not installed (the conformance package is
    dependency-free by design); runs wherever the SDK's [sql] extra is present.
    """
    duckdb = pytest.importorskip("duckdb")
    reserved = {
        row[0]
        for row in duckdb.sql(
            "select keyword_name from duckdb_keywords() "
            "where keyword_category = 'reserved'"
        ).fetchall()
    }
    # true/false/null are YAML scalars, documented as deliberately excluded.
    expected = reserved - {"true", "false", "null"}
    assert _DUCKDB_RESERVED_KEYWORDS == expected


def test_p040_silent_on_embedded_sql_quotes() -> None:
    assert scan_text(_TEMPLATE_SQL_QUOTED, "t.yaml") == []


def test_p040_silent_on_dotted_identifiers_and_expressions() -> None:
    # Dotted identifiers are auto-quoted by the SDK; expressions and
    # non-reserved names never match.
    assert scan_text(_TEMPLATE_CLEAN, "t.yaml") == []


def test_p040_yaml_double_quotes_do_not_protect() -> None:
    # source_query: "order" parses to the same string as source_query: order.
    text = (
        "columns:\n" "  attributes:\n" "    someAttr:\n" '      source_query: "order"\n'
    )
    findings = scan_text(text, "t.yaml")
    assert len(findings) == 1
    assert findings[0].line == 4


def test_p040_yaml_literals_not_flagged() -> None:
    # true/false/null are YAML scalars, not identifiers; they take the
    # transformer's literal path.
    text = (
        "columns:\n"
        "  attributes:\n"
        "    isPrimary:\n"
        "      source_query: true\n"
        "    certificate:\n"
        "      source_query: null\n"
    )
    assert scan_text(text, "t.yaml") == []


def test_p040_only_inside_columns_block() -> None:
    # A source_query under an unrelated top-level key is not graded.
    text = (
        "name: table\n"
        "columns:\n"
        "  attributes:\n"
        "    name:\n"
        "      source_query: table_name\n"
        "other:\n"
        "  source_query: order\n"
    )
    assert scan_text(text, "t.yaml") == []


def test_p040_case_insensitive_keyword_match() -> None:
    text = "columns:\n  attributes:\n    x:\n      source_query: ORDER\n"
    findings = scan_text(text, "t.yaml")
    assert len(findings) == 1


def test_p040_grant_is_not_reserved_in_duckdb() -> None:
    """`grant` is unreserved (`SELECT 1 AS grant` parses) — a plausible column
    name on a governance connector, so flagging it is a false positive."""
    text = "columns:\n  attributes:\n    perm:\n      source_query: grant\n"
    assert scan_text(text, "t.yaml") == []


def test_p040_covers_keywords_added_in_recent_duckdb() -> None:
    """`qualify`, `pivot`, `unpivot`, `lambda`, ... are genuinely reserved."""
    for keyword in ("qualify", "pivot", "unpivot", "lambda", "summarize"):
        text = f"columns:\n  attributes:\n    x:\n      source_query: {keyword}\n"
        assert len(scan_text(text, "t.yaml")) == 1, keyword


def test_p040_skips_non_template_yaml() -> None:
    # A workflow-ish YAML (no source_query key) is never graded, even with
    # reserved words in name: values.
    assert scan_text(_NOT_A_TEMPLATE, ".github/workflows/ci.yaml") == []


def test_p040_suppressed_inline_directive() -> None:
    text = (
        "columns:\n"
        "  attributes:\n"
        "    someAttr:\n"
        "      # conformance: ignore[P040] legacy source column, quoted downstream\n"
        "      source_query: order\n"
    )
    findings = scan_text(text, "t.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed


def test_p040_discover_gates_on_template_shape(tmp_path: Path) -> None:
    (tmp_path / "app" / "transformers").mkdir(parents=True)
    (tmp_path / "app" / "transformers" / "column.yaml").write_text(
        _TEMPLATE_WITH_RESERVED, encoding="utf-8"
    )
    (tmp_path / "compose.yaml").write_text(
        "services:\n  worker:\n    image: x\n", encoding="utf-8"
    )
    gh = tmp_path / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yaml").write_text(_NOT_A_TEMPLATE, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "fixture.yaml").write_text(_TEMPLATE_WITH_RESERVED, encoding="utf-8")

    paths = discover(tmp_path)
    assert [str(p.relative_to(tmp_path)) for p in paths] == [
        "app/transformers/column.yaml"
    ]


def test_p040_scan_path_relativises_file(tmp_path: Path) -> None:
    target = tmp_path / "app" / "transformers" / "column.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(_TEMPLATE_WITH_RESERVED, encoding="utf-8")
    findings = scan_path(target, tmp_path)
    assert findings
    assert findings[0].file == "app/transformers/column.yaml"

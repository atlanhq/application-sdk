"""Meta-tests for the P040 transform-template check.

P040 catches the DuckDB reserved-keyword pattern: the SDK query transformer
renders each template column as ``{source_query} AS {name}`` and only
auto-quotes dotted identifiers, so a bare reserved keyword (``column``,
``order``, ``group``, ...) reaches DuckDB unquoted and every transform of that
entity type fails at runtime with a ParserException on the daft-less SDK
>= 3.22 runtime.  YAML-level quotes do not survive parsing — only embedded SQL
quotes do.

Tests cover the fire path, every documented safe shape (embedded SQL quotes,
dotted identifiers, YAML literals, non-reserved names), scope containment
(only entries inside a ``columns:`` block), discovery gating (only files with
the transform-template shape), and inline suppression.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.transform_templates import discover, scan_path, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_TEMPLATE_WITH_RESERVED = """\
entity: COLUMN
columns:
  - name: attributes.name
    source_query: column_name
  - name: column
    source_query: column
  - name: attributes.order
    source_query: position
"""

_TEMPLATE_SQL_QUOTED = """\
columns:
  - name: '"column"'
    source_query: '"column"'
  - name: '"order"'
    source_query: '"order"'
"""

_TEMPLATE_CLEAN = """\
columns:
  - name: attributes.qualifiedName
    source_query: concat(x, '/', y)
  - name: attributes.description
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
    # Both the name: and the source_query: on the offending column entry.
    assert {f.line for f in findings} == {5, 6}
    assert "ParserException" in findings[0].message
    assert "'\"column\"'" in findings[0].message


def test_p040_silent_on_embedded_sql_quotes() -> None:
    assert scan_text(_TEMPLATE_SQL_QUOTED, "t.yaml") == []


def test_p040_silent_on_dotted_identifiers_and_expressions() -> None:
    # Dotted identifiers are auto-quoted by the SDK; expressions and
    # non-reserved names never match.
    assert scan_text(_TEMPLATE_CLEAN, "t.yaml") == []


def test_p040_yaml_double_quotes_do_not_protect() -> None:
    # name: "column" parses to the same string as name: column — still broken.
    text = 'columns:\n  - name: "column"\n    source_query: src\n'
    findings = scan_text(text, "t.yaml")
    assert len(findings) == 1
    assert findings[0].line == 2


def test_p040_yaml_literals_not_flagged() -> None:
    # true/false/null are YAML scalars, not identifiers; they take the
    # transformer's literal path.
    text = (
        "columns:\n"
        "  - name: attributes.isPrimary\n"
        "    source_query: true\n"
        "  - name: attributes.certificate\n"
        "    source_query: null\n"
    )
    assert scan_text(text, "t.yaml") == []


def test_p040_only_inside_columns_block() -> None:
    # The template's top-level name: key never renders into the SELECT.
    text = (
        "name: table\n"
        "columns:\n"
        "  - name: attributes.name\n"
        "    source_query: table_name\n"
        "other:\n"
        "  name: order\n"
    )
    assert scan_text(text, "t.yaml") == []


def test_p040_case_insensitive_keyword_match() -> None:
    text = "columns:\n  - name: attributes.x\n    source_query: ORDER\n"
    findings = scan_text(text, "t.yaml")
    assert len(findings) == 1


def test_p040_skips_non_template_yaml() -> None:
    # A workflow-ish YAML (no source_query key) is never graded, even with
    # reserved words in name: values.
    assert scan_text(_NOT_A_TEMPLATE, ".github/workflows/ci.yaml") == []


def test_p040_suppressed_inline_directive() -> None:
    text = (
        "columns:\n"
        "  # conformance: ignore[P040] legacy source column, quoted downstream\n"
        "  - name: column\n"
        "    source_query: attributes.src\n"
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

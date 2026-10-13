"""Structure of the per-rule cases files (``RuleDefinition.cases_ref``).

A remediator reads the header, then the index or the one section a finding
names.  A section missing from the index is never found; an index row with
no section is a dead end; a case id a checker emits but the file does not
list sends the remediator nowhere.  Layout: ``conformance/cases/README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conformance.suite.checks.prescriptions import scan_text
from conformance.suite.rules import get_rule, load_catalog
from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.findings import Finding, findings_to_report
from pydantic import ValidationError

import conformance

PACKAGE = Path(conformance.__file__).parent
_ROW = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|(.*)\|\s*$")
_SECTION = re.compile(r"^## (?!Index$)(\S+)\s*$", re.MULTILINE)


def _rules_with_cases() -> list[RuleDefinition]:
    return [r for r in load_catalog() if r.cases_ref]


def _cases_path(rule: RuleDefinition) -> Path:
    assert rule.cases_ref is not None
    return PACKAGE / rule.cases_ref


def _parse(path: Path) -> tuple[str, dict[str, int], set[str]]:
    text = path.read_text()
    header, sep, rest = text.partition("\n## Index\n")
    assert sep, f"{path.name}: no '## Index' heading"
    rows: dict[str, int] = {}
    for line in rest.splitlines():
        if line.startswith("## "):
            break
        if m := _ROW.match(line):
            cells = re.split(r"(?<!\\)\|", m.group(2))
            assert m.group(1) not in rows, f"{path.name}: duplicate row {m.group(1)}"
            rows[m.group(1)] = 1 + len(cells)
    return header, rows, set(_SECTION.findall(rest))


CASE_FIXTURES: dict[str, list[tuple[str, str, str | None]]] = {
    "P001": [
        (
            "app/contracts.py",
            "class TaskStats(Output, allow_unbounded_fields=True):\n"
            "    total: int = 0\n"
            "    name: str = ''\n",
            "remove-opt-out",
        ),
        (
            "app/generated/_input.py",
            "class AppInput(Input, allow_unbounded_fields=True):\n"
            "    tree: dict[str, Any] = {}\n",
            "generated",
        ),
        (
            "app/contracts/_input.py",
            "# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.\n"
            "class AppInput(Input, allow_unbounded_fields=True):\n"
            "    total: int = 0\n",
            "generated",
        ),
        (
            "app/contracts.py",
            "class ExtractInput(Input, allow_unbounded_fields=True):\n"
            "    credentials: dict[str, Any] = {}\n",
            None,
        ),
        (
            "app/contracts.py",
            "class Mixed(Input, allow_unbounded_fields=True):\n"
            "    total: int = 0\n"
            "    shape: SomeAlias = None\n",
            None,
        ),
        (
            "app/contracts.py",
            "class Loose(Input):\n    blob: Any = None\n",
            None,
        ),
    ],
}


def test_some_rule_has_cases() -> None:
    assert _rules_with_cases()


@pytest.mark.parametrize("rule", _rules_with_cases(), ids=lambda r: r.id)
def test_cases_file_index_and_sections_match(rule: RuleDefinition) -> None:
    path = _cases_path(rule)
    assert path.is_file(), f"{rule.id}: cases_ref {rule.cases_ref} does not exist"
    header, rows, sections = _parse(path)
    assert header.strip(), f"{rule.id}: empty header"
    assert rows, f"{rule.id}: index has no rows"
    assert set(rows) == sections, (
        f"{rule.id}: index-only {sorted(set(rows) - sections)}, "
        f"section-only {sorted(sections - set(rows))}"
    )
    bad = {k: n for k, n in rows.items() if n != 3}
    assert not bad, f"{rule.id}: rows without id | condition | how to confirm: {bad}"


@pytest.mark.parametrize("rule", _rules_with_cases(), ids=lambda r: r.id)
def test_every_rule_with_cases_has_fixtures(rule: RuleDefinition) -> None:
    assert rule.id in CASE_FIXTURES, f"{rule.id}: add checker fixtures to CASE_FIXTURES"


@pytest.mark.parametrize(
    ("rule_id", "filename", "src", "expected"),
    [(rid, *fx) for rid, fxs in CASE_FIXTURES.items() for fx in fxs],
)
def test_checker_emits_the_expected_case(
    rule_id: str, filename: str, src: str, expected: str | None
) -> None:
    findings = [f for f in scan_text(src, filename) if f.rule_id == rule_id]
    assert len(findings) == 1, findings
    assert findings[0].case == expected
    if expected is not None:
        _, rows, _ = _parse(_cases_path(get_rule(rule_id)))
        assert expected in rows


def test_case_does_not_change_equality_or_fingerprint() -> None:
    base = Finding(rule_id="P001", file="a.py", line=1, column=1, message="m")
    tagged = Finding(
        rule_id="P001", file="a.py", line=1, column=1, message="m", case="generated"
    )
    assert base == tagged and hash(base) == hash(tagged)

    def _result(f: Finding):
        return (
            findings_to_report([f], tool_version="0", rule_ids={"P001"})
            .runs[0]
            .results[0]
        )

    plain, cased = _result(base), _result(tagged)
    assert plain.partial_fingerprints == cased.partial_fingerprints
    assert "atlan/case" not in plain.properties
    assert cased.properties["atlan/case"] == "generated"


def test_cases_ref_reaches_the_rule_descriptor() -> None:
    props = get_rule("P001").to_reporting_descriptor().properties
    assert props["atlan/casesRef"] == "cases/P001.md"
    assert "atlan/casesRef" not in get_rule("P002").to_reporting_descriptor().properties


def _rule(**overrides) -> RuleDefinition:
    data = get_rule("P001").model_dump()
    data.update(overrides)
    return RuleDefinition.model_validate(data)


def test_cases_ref_is_for_migration_rules_only() -> None:
    with pytest.raises(ValidationError, match="cases_ref is for migration rules"):
        _rule(autofixable=True, remediation_reference=None)

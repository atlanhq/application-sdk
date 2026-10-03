"""Meta-tests for ``RuleDefinition.remediation_reference`` (FND-2481, FND-2230)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from conformance.suite.rules import CATALOG
from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)
from pydantic import ValidationError

import conformance

PACKAGE_ROOT = Path(conformance.__file__).parent
REPO_ROOT = PACKAGE_ROOT.parents[2]
MIGRATION_KINDS = {
    RemediationKind.SKILL,
    RemediationKind.GUIDE,
    RemediationKind.DECISION,
}


def _migration_rules() -> list[RuleDefinition]:
    return [
        r
        for r in CATALOG.values()
        if r.scope in (RuleScope.APP, RuleScope.BOTH) and not r.autofixable
    ]


def _rule(**overrides: object) -> RuleDefinition:
    fields: dict[str, object] = {
        "id": "Z999",
        "name": "Example",
        "tier": EnforcementTier.WARN,
        "mechanism": RuleMechanism.STATIC,
        "scope": RuleScope.APP,
        "category": "example",
    }
    fields.update(overrides)
    return RuleDefinition(**fields)


def test_every_migration_rule_names_a_remediation_reference() -> None:
    """A migration rule is never applied by the lane, so the finding is only
    useful if it says what performs the migration: a skill, a guide, or the
    owner decision that closes it.  No exemption list."""
    missing = sorted(
        r.id for r in _migration_rules() if r.remediation_reference is None
    )
    assert not missing, (
        "app-facing rules with autofixable=False and no remediation_reference — "
        f"name a skill, a guide or a decision: {missing}"
    )


SERIES_AREA = {
    "E": "error-handling",
    "L": "logging",
    "C": "ci",
    "P": "prescriptions",
    "F": "preflight",
    "O": "optimizations",
    "D": "dependency",
    "B": "deprecation",
    "I": "dockerfile",
    "T": "tests",
    "K": "contract-toolkit",
    "S": "security",
}


def _autofixable_app_facing_rules() -> list[RuleDefinition]:
    return [
        r
        for r in CATALOG.values()
        if r.scope in (RuleScope.APP, RuleScope.BOTH) and r.autofixable
    ]


def test_every_autofixable_rule_names_a_remediation_reference() -> None:
    missing = sorted(
        r.id for r in _autofixable_app_facing_rules() if r.remediation_reference is None
    )
    assert not missing, (
        "app-facing auto-fixable rules with no remediation_reference — name the "
        f"area prescription or the fixer command: {missing}"
    )


def test_every_prescription_reference_names_the_rules_bullet() -> None:
    """The reference must be the file the lane actually reads for the rule: its
    series area, carrying the rule's ``**<ID> Name**`` bullet."""
    broken = []
    for r in CATALOG.values():
        ref = r.remediation_reference
        if ref is None or ref.kind is not RemediationKind.PRESCRIPTION:
            continue
        expected = f"programs/areas/{SERIES_AREA[r.id[0]]}.prose.md"
        path = PACKAGE_ROOT / ref.target
        if ref.target != expected or not re.search(
            r"\*\*" + r.id + r"\b", path.read_text() if path.is_file() else ""
        ):
            broken.append(f"{r.id}->{ref.target}")
    assert (
        not broken
    ), f"prescription references that miss the rule's bullet: {sorted(broken)}"


def test_every_command_reference_names_a_cli_command() -> None:
    """``[uvx|uv run] atlan-application-sdk-conformance[==<version>] <command> ...``,
    where ``<command>`` is a real subcommand.  A pin is allowed because a bare
    command can resolve a stale locked version and leave the finding (FND-607)."""
    from conformance.cli import _COMMANDS

    package = re.compile(r"^atlan-application-sdk-conformance(==\S+)?$")
    broken = []
    for r in CATALOG.values():
        ref = r.remediation_reference
        if ref is None or ref.kind is not RemediationKind.COMMAND:
            continue
        parts = ref.target.split()
        if parts[:1] == ["uvx"]:
            parts = parts[1:]
        elif parts[:2] == ["uv", "run"]:
            parts = parts[2:]
        if len(parts) < 2 or not package.match(parts[0]) or parts[1] not in _COMMANDS:
            broken.append(f"{r.id}->{ref.target}")
    assert (
        not broken
    ), f"command references that are not a conformance CLI command: {sorted(broken)}"


def test_migration_rule_rejects_a_mechanical_reference_kind() -> None:
    with pytest.raises(ValidationError, match="migration rule"):
        _rule(
            autofixable=False,
            remediation_reference=RemediationReference(
                kind=RemediationKind.COMMAND, target="bootstrap --resync"
            ),
        )


def test_autofixable_rule_rejects_a_migration_reference_kind() -> None:
    with pytest.raises(ValidationError, match="auto-fixable rule"):
        _rule(
            autofixable=True,
            remediation_reference=RemediationReference(
                kind=RemediationKind.SKILL, target="migrate-off-daft"
            ),
        )


def test_decision_reference_requires_a_note() -> None:
    with pytest.raises(ValidationError, match="note"):
        RemediationReference(kind=RemediationKind.DECISION, target="app owner")


def test_every_skill_reference_resolves_to_a_packaged_skill() -> None:
    """A skill that is not in the wheel cannot be reached from a connector repo."""
    broken = sorted(
        f"{r.id}->{r.remediation_reference.target}"
        for r in CATALOG.values()
        if r.remediation_reference is not None
        and r.remediation_reference.kind is RemediationKind.SKILL
        and not (
            PACKAGE_ROOT / "skills" / r.remediation_reference.target / "SKILL.md"
        ).is_file()
    )
    assert not broken, f"skill references with no packaged SKILL.md: {broken}"


def test_every_guide_reference_resolves_to_a_packaged_file_that_names_the_rule() -> (
    None
):
    broken = []
    for r in CATALOG.values():
        ref = r.remediation_reference
        if ref is None or ref.kind is not RemediationKind.GUIDE:
            continue
        path = PACKAGE_ROOT / ref.target
        if not path.is_file() or not re.search(rf"\b{r.id}\b", path.read_text()):
            broken.append(f"{r.id}->{ref.target}")
    assert (
        not broken
    ), f"guide references that do not ship or do not name the rule: {sorted(broken)}"


def test_every_packaged_skill_names_the_rules_that_point_at_it() -> None:
    """Two-way link: a skill lists the rules it clears, so it can re-detect them
    when it finishes, and so a rule cannot point at a skill that does not know it."""
    missing = []
    for r in CATALOG.values():
        ref = r.remediation_reference
        if ref is None or ref.kind is not RemediationKind.SKILL:
            continue
        skill = PACKAGE_ROOT / "skills" / ref.target / "SKILL.md"
        if skill.is_file() and not re.search(rf"\b{r.id}\b", skill.read_text()):
            missing.append(f"{ref.target}:{r.id}")
    assert (
        not missing
    ), f"skills that do not name a rule pointing at them: {sorted(missing)}"


def test_repo_root_skill_paths_resolve_to_the_packaged_skill() -> None:
    """Developers in this repo keep reaching the skill at its old path."""
    for skill in sorted((PACKAGE_ROOT / "skills").iterdir()):
        root_copy = REPO_ROOT / ".claude" / "skills" / skill.name / "SKILL.md"
        assert root_copy.is_file(), root_copy
        assert root_copy.resolve() == (skill / "SKILL.md").resolve(), root_copy


def test_remediation_reference_rides_the_sarif_wire() -> None:
    rule = next(r for r in _migration_rules() if r.remediation_reference is not None)
    props = rule.to_reporting_descriptor().properties
    ref = rule.remediation_reference
    assert ref is not None
    assert props["atlan/remediationReference"] == {
        "kind": ref.kind.value,
        "target": ref.target,
        "note": ref.note,
    }


def test_rule_without_a_reference_omits_the_sarif_property() -> None:
    rule = next(r for r in CATALOG.values() if r.remediation_reference is None)
    assert "atlan/remediationReference" not in rule.to_reporting_descriptor().properties


def test_rule_docs_print_the_remediation_reference(tmp_path: Path) -> None:
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    for r in _migration_rules():
        text = (tmp_path / "by-id" / f"{r.id}.md").read_text()
        assert re.search(r"\*\*(Migrate with|Decision):\*\*", text), r.id


def test_skills_dir_prints_the_packaged_skills_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from conformance import cli

    monkeypatch.setattr(
        sys, "argv", ["atlan-application-sdk-conformance", "skills-dir"]
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert Path(out).resolve() == (PACKAGE_ROOT / "skills").resolve()


@pytest.mark.parametrize(
    "path",
    [
        "programs/functions/detect-violations.prose.md",
        "programs/functions/remediate-finding.prose.md",
        "programs/patterns/detect-fix-recheck.prose.md",
        "bootstrap/templates/remediate.md",
    ],
)
def test_remediation_programs_carry_the_reference(path: str) -> None:
    assert "remediation_reference" in (PACKAGE_ROOT / path).read_text(), path


@pytest.mark.parametrize(
    "path",
    [
        PACKAGE_ROOT / "bootstrap" / "templates" / "remediate.md",
        REPO_ROOT / ".claude" / "skills" / "remediate" / "SKILL.md",
    ],
)
def test_remediate_skill_resolves_skills_dir_for_the_hand_off(path: Path) -> None:
    assert "skills-dir" in path.read_text(), path

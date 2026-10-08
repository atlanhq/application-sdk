"""Meta-tests for ``RuleDefinition.remediation_reference`` (FND-2481, FND-2230)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from conformance.suite.rules import CATALOG, get_rule
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


def _packaged_skills() -> list[Path]:
    return sorted(d for d in (PACKAGE_ROOT / "skills").iterdir() if d.is_dir())


def _skill_order() -> list[str]:
    return (PACKAGE_ROOT / "skills" / "order.txt").read_text().split()


def _rules_by_skill() -> dict[str, set[str]]:
    by_skill: dict[str, set[str]] = {}
    for r in CATALOG.values():
        ref = r.remediation_reference
        if ref is not None and ref.kind is RemediationKind.SKILL:
            by_skill.setdefault(ref.target, set()).add(r.id)
    return by_skill


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


def _BULLET_RE(rule_id: str) -> re.Pattern[str]:
    """A ``**<ID> Name**`` entry at the start of a line, optionally as a list
    item; a bare ``**<ID>**`` mention elsewhere does not count."""
    named = r"\*\*[A-Z]\d{3} [^*\n]+\*\*"
    return re.compile(
        rf"(?m)^\s*(?:-\s+)?(?:{named}\s*/\s*)*\*\*{re.escape(rule_id)} [^*\n]+\*\*"
    )


def test_bullet_pattern_rejects_a_bare_id_mention() -> None:
    assert _BULLET_RE("C001").search("**C001 PinActionsToSha** — ...")
    assert _BULLET_RE("B006").search("- **B006 StaleContractLedger** (writes ...)")
    assert _BULLET_RE("T012").search(
        "- **T011 MissingIntegrationTestSuite** / **T012 MissingE2ETestSuite** — no"
    )
    assert not _BULLET_RE("C001").search("see **C001** for pinning")


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
        text = path.read_text() if path.is_file() else ""
        if ref.target != expected or not _BULLET_RE(r.id).search(text):
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
    for skill in _packaged_skills():
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


def test_skill_order_lists_every_packaged_skill_once() -> None:
    order = _skill_order()
    assert len(order) == len(set(order)), order
    assert sorted(order) == [d.name for d in _packaged_skills()]


def test_skill_order_honours_each_skills_runs_before() -> None:
    """A skill that must run before another (``runs_before`` in its frontmatter)
    comes first in ``order.txt``: ``migrate-off-daft`` has to cross the daft
    cliff before any skill that bumps the SDK."""
    order = _skill_order()
    broken = []
    for skill in _packaged_skills():
        for later in _frontmatter_list(skill, "runs_before"):
            if later in order and order.index(later) < order.index(skill.name):
                broken.append(f"{skill.name} must run before {later}")
    assert not broken, broken


def test_each_skill_rechecks_exactly_the_rules_that_point_at_it() -> None:
    """The re-check after a hand-off is ``detect --rule <ids>`` for the rules the
    skill clears, so it reports nothing the skill does not touch."""
    for name, ids in _rules_by_skill().items():
        text = (PACKAGE_ROOT / "skills" / name / "SKILL.md").read_text()
        match = re.search(r"detect --rule ([A-Z0-9,]+)", text)
        assert match, name
        assert set(match.group(1).split(",")) == ids, name


def test_remediation_reference_round_trips_through_sarif() -> None:
    from conformance.suite.schema.extensions import AtlanRuleProperties

    for r in CATALOG.values():
        props = r.to_reporting_descriptor().properties
        assert (
            AtlanRuleProperties.from_properties(props).remediation_reference
            == r.remediation_reference
        ), r.id


@pytest.mark.parametrize(
    "malformed",
    [
        {"kind": "rewrite", "target": "x", "note": ""},
        {"kind": "decision", "target": "app owner", "note": ""},
        {"kind": "skill", "note": ""},
    ],
)
def test_malformed_sarif_reference_is_rejected(malformed: dict[str, str]) -> None:
    from conformance.suite.schema.extensions import AtlanRuleProperties

    props = dict(get_rule("F001").to_reporting_descriptor().properties)
    props["atlan/remediationReference"] = malformed
    with pytest.raises(ValidationError):
        AtlanRuleProperties.from_properties(props)


def _frontmatter_list(skill: Path, key: str) -> list[str]:
    """A list-valued frontmatter key, in flow (``[a, b]``) or block (``- a``) style."""
    lines = (skill / "SKILL.md").read_text().split("---", 2)[1].splitlines()
    for i, line in enumerate(lines):
        match = re.match(rf"^{key}:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return [v.strip() for v in value.strip("[]").split(",") if v.strip()]
        items = []
        for nxt in lines[i + 1 :]:
            item = re.match(r"^\s+-\s+(.+)$", nxt)
            if not item:
                break
            items.append(item.group(1).strip().strip("\"'"))
        return items
    return []


def test_frontmatter_lists_parse_in_both_styles(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: x\nruns_before: [a, b]\nalso_clears:\n  - c\n  - d\n---\nbody\n"
    )
    assert _frontmatter_list(tmp_path, "runs_before") == ["a", "b"]
    assert _frontmatter_list(tmp_path, "also_clears") == ["c", "d"]


def test_every_route_reaches_a_skill_that_will_run_it() -> None:
    """/remediate starts a skill only for its own rules or for a symbol in its
    ``also_clears`` list. A routed site can produce no finding of the
    receiver's own (a ``tests/`` site P005 reports but B008 does not scan), so
    every receiver declares the symbols it clears for others."""
    broken = []
    for skill in _packaged_skills():
        for target in _frontmatter_list(skill, "routes_to"):
            target_dir = PACKAGE_ROOT / "skills" / target
            if not target_dir.is_dir():
                broken.append(f"{skill.name} routes to unpackaged {target}")
            elif not _frontmatter_list(target_dir, "also_clears"):
                broken.append(
                    f"{skill.name} routes to {target}, which declares no also_clears"
                )
    assert not broken, broken


def test_also_clears_symbols_are_named_in_the_routing_skill() -> None:
    """Every symbol a skill clears for others is one a routing skill sends it."""
    missing = []
    for skill in _packaged_skills():
        symbols = _frontmatter_list(skill, "also_clears")
        if not symbols:
            continue
        senders = [
            s
            for s in _packaged_skills()
            if skill.name in _frontmatter_list(s, "routes_to")
        ]
        text = "".join((s / "SKILL.md").read_text() for s in senders)
        missing += [f"{skill.name}:{sym}" for sym in symbols if sym not in text]
    assert not missing, missing


def test_each_route_matches_the_symbols_its_sender_names() -> None:
    """Checked per route, not over all senders' prose at once: a sender that
    names a receiver's symbol declares the route, and a declared route names at
    least one of the receiver's symbols. Otherwise a sender could drop a route
    while another sender's prose still covers the symbol."""
    skills = _packaged_skills()
    clears = {s.name: _frontmatter_list(s, "also_clears") for s in skills}
    broken = []
    for sender in skills:
        text = (sender / "SKILL.md").read_text()
        routes = _frontmatter_list(sender, "routes_to")
        for receiver, symbols in clears.items():
            if receiver == sender.name or not symbols:
                continue
            named = [sym for sym in symbols if sym in text]
            if named and receiver not in routes:
                broken.append(
                    f"{sender.name} names {named} but does not route to {receiver}"
                )
            if receiver in routes and not named:
                broken.append(
                    f"{sender.name} routes to {receiver} but names none of its symbols"
                )
    assert not broken, broken

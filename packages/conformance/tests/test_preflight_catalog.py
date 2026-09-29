from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

BLOCKING = {"F006", "F007"}
#: Retired in 0.39.0 and deleted in 0.40.0; the ids are never reused.
DELETED = {"F017", "F018"}
PREFLIGHT_IDS = [
    f"F{number:03}" for number in range(1, 21) if f"F{number:03}" not in DELETED
]


def test_preflight_contract_rules_have_evidence_based_enforcement():
    for rule_id in PREFLIGHT_IDS[5:]:
        rule = get_rule(rule_id)
        assert rule.tier is (
            EnforcementTier.BLOCK if rule_id in BLOCKING else EnforcementTier.WARN
        )
        assert rule.scope is RuleScope.APP
        assert rule.until is None
        assert rule.help_uri
        assert rule.rationale


def test_no_preflight_rule_executes_tests():
    """Conformance checks the scenarios are defined; the test gate runs them."""
    for rule_id in PREFLIGHT_IDS:
        assert get_rule(rule_id).mechanism is RuleMechanism.STATIC


def test_every_preflight_rule_links_to_a_packaged_investigation_section():
    from importlib.resources import files

    from conformance.suite.rules.preflight import RULES

    guide = files("conformance").joinpath("docs/preflight-guide.md").read_text()
    assert [rule.id for rule in RULES] == PREFLIGHT_IDS
    for rule in RULES:
        assert f"## {rule.id}\n" in guide
        assert f"preflight-guide.md#{rule.id.lower()}" in rule.full_description
        section = guide.split(f"## {rule.id}\n", 1)[1].split("\n## ", 1)[0]
        for requirement in (
            "**Contract:**",
            "**Investigate:**",
            "**Fix:**",
            "**Verify:**",
        ):
            assert requirement in section, (rule.id, requirement)

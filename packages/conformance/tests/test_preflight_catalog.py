from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)


def test_preflight_contract_rules_have_evidence_based_enforcement():
    for number in range(52, 67):
        rule = get_rule(f"P{number:03}")
        assert rule.tier is (
            EnforcementTier.BLOCK
            if number in {52, 53, 62, 63, 64, 66}
            else EnforcementTier.WARN
        )
        assert rule.scope is (RuleScope.SDK if number in {63, 64} else RuleScope.APP)
        assert rule.mechanism is (
            RuleMechanism.TEST if number in {62, 63, 64} else RuleMechanism.STATIC
        )
        assert rule.help_uri
        assert rule.rationale


def test_every_preflight_rule_links_to_a_packaged_investigation_section():
    from importlib.resources import files

    from conformance.suite.rules.preflight import RULES

    guide = files("conformance").joinpath("docs/preflight-guide.md").read_text()
    assert len(RULES) == 20
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

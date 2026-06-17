"""Tests for the rule catalog and RuleDefinition model."""

from __future__ import annotations

import re

import pytest
from conformance.suite.rules import CATALOG, _combine_rules, get_rule
from conformance.suite.schema import load_catalog
from conformance.suite.schema.catalog import RuleDefinition, validate_catalog
from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism
from pydantic import ValidationError


def test_catalog_loads_without_error() -> None:
    """The catalog loads and validates cleanly."""
    rules = load_catalog()
    assert len(rules) > 0


def test_catalog_no_duplicate_ids() -> None:
    """Every rule ID in the catalog is unique."""
    rules = load_catalog()
    ids = [r.id for r in rules]
    assert len(ids) == len(
        set(ids)
    ), f"Duplicate rule IDs: {[x for x in ids if ids.count(x) > 1]}"


def test_catalog_ids_match_pattern() -> None:
    """All rule IDs match the expected namespace pattern (letter + 3 digits)."""
    rules = load_catalog()
    pattern = re.compile(r"^[A-Z]\d{3}$")
    bad = [r.id for r in rules if not pattern.match(r.id)]
    assert not bad, f"Rule IDs with unexpected format: {bad}"


def test_catalog_all_have_required_fields() -> None:
    """Every rule has a non-empty id, name, tier, mechanism, and category."""
    rules = load_catalog()
    for rule in rules:
        assert rule.id, f"Rule missing id: {rule}"
        assert rule.name, f"Rule {rule.id} missing name"
        assert isinstance(
            rule.tier, EnforcementTier
        ), f"Rule {rule.id} has invalid tier"
        assert isinstance(
            rule.mechanism, RuleMechanism
        ), f"Rule {rule.id} has invalid mechanism"
        assert rule.category, f"Rule {rule.id} missing category"


def test_catalog_e_series_present() -> None:
    """The E-series error-handling rules are all present."""
    rules = load_catalog()
    e_ids = {r.id for r in rules if r.id.startswith("E")}
    expected = {
        "E001",
        "E002",
        "E003",
        "E004",
        "E005",
        "E006",
        "E007",
        "E008",
        "E009",
        "E010",
        "E011",
        "E012",
        "E013",
        "E014",
        "E015",
        "E016",
        "E017",
        "E018",
    }
    missing = expected - e_ids
    assert not missing, f"Missing E-series rules: {missing}"


def test_catalog_l_series_present() -> None:
    """The L-series logging rules are all present."""
    rules = load_catalog()
    l_ids = {r.id for r in rules if r.id.startswith("L")}
    expected = {
        "L001",
        "L002",
        "L003",
        "L004",
        "L005",
        "L006",
        "L007",
        "L008",
        "L009",
        "L010",
        "L011",
        "L012",
        "L013",
        "L014",
        "L015",
        "L016",
        "L017",
        "L018",
        "L024",
    }
    missing = expected - l_ids
    assert not missing, f"Missing L-series rules: {missing}"


def test_catalog_c_series_present() -> None:
    """The C-series CI/workflow supply-chain rules are all present."""
    rules = load_catalog()
    c_ids = {r.id for r in rules if r.id.startswith("C")}
    expected = {"C001"}
    missing = expected - c_ids
    assert not missing, f"Missing C-series rules: {missing}"


def test_catalog_is_mapping_keyed_by_id() -> None:
    """CATALOG is a Mapping whose keys equal each rule's id."""
    from collections.abc import Mapping

    assert isinstance(CATALOG, Mapping)
    for rule_id, rule in CATALOG.items():
        assert rule_id == rule.id


def test_get_rule_c001() -> None:
    """get_rule('C001') returns the C001 RuleDefinition."""
    rule = get_rule("C001")
    assert isinstance(rule, RuleDefinition)
    assert rule.id == "C001"
    assert rule.name == "UnpinnedActionReference"


def test_get_rule_missing_raises_key_error() -> None:
    """get_rule for an unknown ID raises KeyError."""
    with pytest.raises(KeyError):
        get_rule("NONEXISTENT")


def test_to_reporting_descriptor_roundtrip() -> None:
    """RuleDefinition → ReportingDescriptor preserves tier and mechanism in properties."""
    p001 = get_rule("E001")
    descriptor = p001.to_reporting_descriptor()

    assert descriptor.id == "E001"
    assert descriptor.name == "BareExceptPass"
    assert descriptor.default_configuration.level == "error"  # block → error
    assert descriptor.properties["atlan/tier"] == "block"
    assert descriptor.properties["atlan/mechanism"] == "static"
    assert descriptor.properties["atlan/category"] == "silent-swallow"
    assert descriptor.properties["atlan/autofixable"] is False
    assert descriptor.properties["atlan/orthogonalGate"] == "tests"


def test_warn_tier_maps_to_warning_level() -> None:
    """A warn-tier rule produces defaultConfiguration.level='warning'."""
    # P003 (BroadContextlibSuppress) is tier=warn
    p003 = get_rule("E003")
    descriptor = p003.to_reporting_descriptor()
    assert descriptor.default_configuration.level == "warning"


def test_block_tier_maps_to_error_level() -> None:
    """A block-tier rule produces defaultConfiguration.level='error'."""
    p001 = get_rule("E001")
    descriptor = p001.to_reporting_descriptor()
    assert descriptor.default_configuration.level == "error"


def test_duplicate_id_raises() -> None:
    """_combine_rules() raises ValueError on duplicate IDs."""
    r1 = RuleDefinition(
        id="E001",
        name="R1",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="test",
    )
    r2 = RuleDefinition(
        id="E001",
        name="R2",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test",
    )
    with pytest.raises(ValueError, match="duplicate rule ID"):
        _combine_rules((r1,), (r2,))


def test_invalid_rule_id_raises() -> None:
    """A rule ID that doesn't match the pattern raises ValidationError."""
    with pytest.raises(ValidationError):
        RuleDefinition(
            id="BADID",  # should be letter + 3 digits
            name="BadRule",
            tier=EnforcementTier.BLOCK,
            mechanism=RuleMechanism.STATIC,
            category="test",
        )


def test_validate_catalog_raises_on_duplicate() -> None:
    """validate_catalog raises ValueError on duplicate IDs."""
    r1 = RuleDefinition(
        id="E001",
        name="R1",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="test",
    )
    r2 = RuleDefinition(
        id="E001",
        name="R2",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="test",
    )
    with pytest.raises(ValueError, match="duplicate rule ID"):
        validate_catalog([r1, r2])

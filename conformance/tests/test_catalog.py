"""Tests for the rule catalog loader and RuleDefinition model."""

import pytest

from conformance.schema import load_catalog
from conformance.schema.catalog import RuleDefinition
from conformance.schema.disposition import EnforcementTier, RuleMechanism


def test_catalog_loads_without_error():
    """The bundled catalog.yaml loads and validates cleanly."""
    rules = load_catalog()
    assert len(rules) > 0


def test_catalog_no_duplicate_ids():
    """Every rule ID in the catalog is unique."""
    rules = load_catalog()
    ids = [r.id for r in rules]
    assert len(ids) == len(
        set(ids)
    ), f"Duplicate rule IDs: {[x for x in ids if ids.count(x) > 1]}"


def test_catalog_ids_match_pattern():
    """All rule IDs match the expected namespace pattern (letter + 3 digits)."""
    import re

    rules = load_catalog()
    pattern = re.compile(r"^[A-Z]\d{3}$")
    bad = [r.id for r in rules if not pattern.match(r.id)]
    assert not bad, f"Rule IDs with unexpected format: {bad}"


def test_catalog_all_have_required_fields():
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


def test_catalog_p_series_present():
    """The P-series error-recovery rules are all present."""
    rules = load_catalog()
    p_ids = {r.id for r in rules if r.id.startswith("P")}
    expected = {
        "P001",
        "P002",
        "P003",
        "P004",
        "P005",
        "P006",
        "P007",
        "P008",
        "P009",
        "P010",
        "P012",
        "P013",
    }
    missing = expected - p_ids
    assert not missing, f"Missing P-series rules: {missing}"


def test_catalog_l_series_present():
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


def test_to_reporting_descriptor_roundtrip():
    """RuleDefinition → ReportingDescriptor preserves tier and mechanism in properties."""
    rules = load_catalog()
    p001 = next(r for r in rules if r.id == "P001")
    descriptor = p001.to_reporting_descriptor()

    assert descriptor.id == "P001"
    assert descriptor.name == "BareExceptPass"
    assert descriptor.default_configuration.level == "error"  # block → error
    assert descriptor.properties["atlan/tier"] == "block"
    assert descriptor.properties["atlan/mechanism"] == "static"
    assert descriptor.properties["atlan/category"] == "silent-swallow"
    assert descriptor.properties["atlan/autofixable"] is False
    assert descriptor.properties["atlan/orthogonalGate"] == "tests"


def test_warn_tier_maps_to_warning_level():
    """A warn-tier rule produces defaultConfiguration.level='warning'."""
    rules = load_catalog()
    # P003 (BroadContextlibSuppress) is tier=warn
    p003 = next(r for r in rules if r.id == "P003")
    descriptor = p003.to_reporting_descriptor()
    assert descriptor.default_configuration.level == "warning"


def test_block_tier_maps_to_error_level():
    """A block-tier rule produces defaultConfiguration.level='error'."""
    rules = load_catalog()
    p001 = next(r for r in rules if r.id == "P001")
    descriptor = p001.to_reporting_descriptor()
    assert descriptor.default_configuration.level == "error"


def test_duplicate_id_raises():
    """Loading a catalog with duplicate rule IDs raises ValueError."""
    import tempfile
    from pathlib import Path

    catalog_yaml = """
- id: P001
  name: Rule1
  tier: block
  mechanism: static
  category: test
  short_description: "first"
- id: P001
  name: Rule2Duplicate
  tier: warn
  mechanism: static
  category: test
  short_description: "duplicate"
"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(catalog_yaml)
        tmp_path = Path(f.name)

    with pytest.raises(ValueError, match="Duplicate rule ID"):
        load_catalog(tmp_path)

    tmp_path.unlink()


def test_invalid_rule_id_raises():
    """A rule ID that doesn't match the pattern raises ValidationError."""
    with pytest.raises(Exception):
        RuleDefinition(
            id="BADID",  # should be letter + 3 digits
            name="BadRule",
            tier=EnforcementTier.BLOCK,
            mechanism=RuleMechanism.STATIC,
            category="test",
        )

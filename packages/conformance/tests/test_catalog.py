"""Tests for the rule catalog and RuleDefinition model."""

from __future__ import annotations

import re

import pytest
from conformance.suite.rules import CATALOG, _combine_rules, get_rule
from conformance.suite.schema import load_catalog
from conformance.suite.schema.catalog import RuleDefinition, validate_catalog
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)
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


def test_catalog_all_have_rationale() -> None:
    """Every rule in the catalog must have a non-empty rationale."""
    rules = load_catalog()
    missing = [rule.id for rule in rules if not rule.rationale.strip()]
    assert (
        not missing
    ), f"Rules missing rationale (add a rationale= to each RuleDefinition): {missing}"


def test_catalog_all_have_scope() -> None:
    """Every rule must declare a valid RuleScope (sdk / app / both)."""
    rules = load_catalog()
    bad = [rule.id for rule in rules if not isinstance(rule.scope, RuleScope)]
    assert not bad, f"Rules with invalid/missing scope: {bad}"


def test_scope_is_required_field() -> None:
    """``scope`` has no default: constructing a rule without it must fail.

    This is what makes ``test_catalog_all_have_scope`` an enforceable guarantee
    — a new rule that forgets ``scope=`` cannot even be constructed.
    """
    with pytest.raises(ValidationError):
        RuleDefinition(  # pyright: ignore[reportCallIssue]  # scope deliberately omitted
            id="E999",
            name="NoScope",
            tier=EnforcementTier.WARN,
            mechanism=RuleMechanism.STATIC,
            category="test",
        )


def test_catalog_app_scoped_rules_are_the_expected_set() -> None:
    """The one-sided rules declare app/sdk scope; everything else is 'both'.

    APP-scoped rules (dependency pinning, managed-workflow drift, Dockerfile
    conformance, orchestration-seam P004/P005, deprecated-symbol usage B001)
    must never fire on the SDK itself, which publishes the contract.  Pin the
    exact set so a new rule has to make a deliberate scope decision rather than
    silently inheriting.

    Note C003 (.gitignore entries) is *both*, not app: the SDK has its own
    .gitignore sharing the standard baseline, so the rule is useful there too —
    only C002 (bootstrap workflow drift) is genuinely 0%-applicable to the SDK.

    I001–I005 (Dockerfile conformance) are app-scoped because the SDK Dockerfile
    *builds* the base image that these rules enforce, so the rules are meaningless
    and noisy when applied to the SDK itself.

    P004–P005 (orchestration-seam) are app-scoped: apps must reach Temporal
    through the SDK seam (BLDX-1417).  P006–P007 are SDK-only: the SDK must
    keep Temporal contained behind its seam.

    P017–P018 (entrypoint-conformance) are app-scoped: the SDK's ``main.py``
    legitimately calls ``create_worker`` and ``uvicorn.run`` — that is its job;
    consumer apps must delegate those calls to the SDK launcher (BLDX-1411).

    B001 (deprecated-symbol usage) is app-scoped: the SDK deliberately retains
    and internally uses its own deprecated shims.  B002–B004 (deprecation
    authoring hygiene) are SDK-only — they grade how the SDK *declares* its
    deprecations, which is only meaningful on the publisher.
    """
    rules = load_catalog()
    app_scoped = {r.id for r in rules if r.scope == RuleScope.APP}
    # C002/D001/D002: publisher-side contract. D004/D005: the same
    # redeclaration/extra contract on dependency-groups and SDK extras.
    # D006/D007/D008: the app pyproject baseline (python floor, build backend,
    # type-checking) the SDK publishes. P004/P005: apps must reach the
    # orchestration layer through the SDK seam, not Temporal/SDK-internals
    # (BLDX-1417). P008–P012: apps must use the SDK's storage seam, not
    # hand-roll object stores or bare path fields (BLDX-1398).
    # P013/P014: apps must declare typed Input/Output contracts on all
    # entrypoints and tasks (BLDX-1413). P015: contract fields should use
    # typed models, not containers of primitives (BLDX-1413).
    # P016: entry-point contract/code alignment — only apps have a Pkl contract
    # and app/generated/ dirs; the SDK itself has no @entrypoint-decorated App
    # methods and no contract to drift from (BLDX-1425).
    # P017/P018: apps must boot through the SDK launcher, not hand-roll
    # workers or servers (BLDX-1411).
    # P025: app-name alignment — only apps have an atlan.yaml and .env.example;
    # the SDK has neither, so this check is meaningless there (BLDX-1491).
    # P029/P030: SDR-readiness — only apps declare self_deployed_runtime; the SDK
    # itself never does, so these are APP-scoped (DISTR-752).
    # T002/T003: SDR test-quality — apps that declare SDR must have an SDR test
    # class; the SDK itself is not an SDR app (DISTR-752).
    # I001–I005: Dockerfile conformance (SDK builds the base image, not consuming it).
    # B001: consuming a deprecated SDK symbol (BLDX-1418).
    # O002/O003/O004: asset-mapper usage — connectors build assets with pyatlan_v9,
    # serialize with to_nested_bytes, and type their mapper returns (BLDX-1492); the
    # SDK is the framework, not a connector.
    assert app_scoped == {
        "B001",
        "C002",
        "D001",
        "D002",
        "D004",
        "D005",
        "D006",
        "D007",
        "D008",
        "P004",
        "P005",
        "P008",
        "P009",
        "P010",
        "P011",
        "P012",
        "P013",
        "P014",
        "P015",
        "P016",
        "P017",
        "P018",
        "P025",
        "P029",
        "P030",
        "T002",
        "T003",
        "O002",
        "O003",
        "O004",
        "I001",
        "I002",
        "I003",
        "I004",
        "I005",
    }, app_scoped
    # SDK-only rules: the SDK must keep Temporal contained behind its seam
    # (P006/P007, BLDX-1417) and declare its deprecations correctly (B002–B004).
    sdk_scoped = {r.id for r in rules if r.scope == RuleScope.SDK}
    assert sdk_scoped == {"B002", "B003", "B004", "P006", "P007"}, sdk_scoped
    both = {r.id for r in rules if r.scope == RuleScope.BOTH}
    assert both == {r.id for r in rules} - app_scoped - sdk_scoped


def test_scope_emitted_in_sarif_properties() -> None:
    """The rule's scope is surfaced as ``atlan/scope`` in SARIF properties."""
    descriptor = get_rule("D001").to_reporting_descriptor()
    assert descriptor.properties["atlan/scope"] == "app"
    descriptor = get_rule("E001").to_reporting_descriptor()
    assert descriptor.properties["atlan/scope"] == "both"


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
    """The L-series logging rules are all present (contiguous L001–L018)."""
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
        "L019",
        "L020",
        "L021",
    }
    missing = expected - l_ids
    assert not missing, f"Missing L-series rules: {missing}"
    # Stricter than the other series tests (not-missing only): the L-series was
    # renumbered in PR #2191 (L013→L012 etc.) and stale suppressions referencing
    # the old IDs would silently pass a not-missing check.
    extra = l_ids - expected
    assert not extra, f"Unexpected L-series rules: {extra}"


def test_catalog_c_series_present() -> None:
    """The C-series CI/workflow supply-chain rules are all present."""
    rules = load_catalog()
    c_ids = {r.id for r in rules if r.id.startswith("C")}
    expected = {"C001", "C002"}
    missing = expected - c_ids
    assert not missing, f"Missing C-series rules: {missing}"


def test_catalog_d_series_present() -> None:
    """The D-series dependency rules are all present."""
    rules = load_catalog()
    d_ids = {r.id for r in rules if r.id.startswith("D")}
    expected = {"D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008"}
    missing = expected - d_ids
    assert not missing, f"Missing D-series rules: {missing}"


def test_catalog_p_series_present() -> None:
    """The P-series prescription rules are exactly P001–P025.

    Strict equality (not just not-missing): P004–P007 are the orchestration-seam
    rules (BLDX-1417); P008–P012 are the storage-seam rules (BLDX-1398);
    P013–P015 are the typed-contract-boundary rules (BLDX-1413);
    P016 is the entry-point contract/code alignment rule (BLDX-1425);
    P017–P018 are the entrypoint-conformance rules (BLDX-1411);
    P019 is the client-seam rule — raw HTTP to Atlan instead of pyatlan
    (BLDX-1430).  P020–P024 are the determinism / async-correctness rules:
    non-deterministic primitives, side-effect I/O, un-awaited coroutines,
    blocking calls in async defs, and pyatlan sync ``AtlanClient`` use.
    P025 is the app-name alignment rule — code name, atlan.yaml name:, and
    .env.example ATLAN_APPLICATION_NAME must agree (BLDX-1491).
    P026–P028 are reserved by PR #2417 (GetattrOnTypedContractField,
    AppStateAsCrossTaskChannel, ManualQualifiedNameFString).
    P029/P030 are the SDR-readiness rules — manifest agent_json slot and
    upload call presence (DISTR-752).
    A stray or renumbered P-id would slip past a subset check while
    breaking fleet-wide ``# conformance: ignore[Pxxx]`` suppressions.
    """
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
        "P011",
        "P012",
        "P013",
        "P014",
        "P015",
        "P016",
        "P017",
        "P018",
        "P019",
        "P020",
        "P021",
        "P022",
        "P023",
        "P024",
        "P025",
        "P029",
        "P030",
    }
    missing = expected - p_ids
    assert not missing, f"Missing P-series rules: {missing}"
    extra = p_ids - expected
    assert not extra, f"Unexpected P-series rules: {extra}"


def test_catalog_o_series_present() -> None:
    """The O-series optimisation rules are all present."""
    rules = load_catalog()
    o_ids = {r.id for r in rules if r.id.startswith("O")}
    expected = {"O001", "O002", "O003", "O004"}
    missing = expected - o_ids
    assert not missing, f"Missing O-series rules: {missing}"


def test_catalog_t_series_present() -> None:
    """The T-series test-quality rules are all present."""
    rules = load_catalog()
    t_ids = {r.id for r in rules if r.id.startswith("T")}
    expected = {"T001", "T002", "T003"}
    missing = expected - t_ids
    assert not missing, f"Missing T-series rules: {missing}"


def test_catalog_b_series_present() -> None:
    """The B-series backwards-compatibility / deprecation rules are all present."""
    rules = load_catalog()
    b_ids = {r.id for r in rules if r.id.startswith("B")}
    expected = {"B001", "B002", "B003", "B004", "B005", "B006"}
    missing = expected - b_ids
    assert not missing, f"Missing B-series rules: {missing}"
    extra = b_ids - expected
    assert not extra, f"Unexpected B-series rules: {extra}"


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
        scope=RuleScope.BOTH,
        category="test",
    )
    r2 = RuleDefinition(
        id="E001",
        name="R2",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        scope=RuleScope.BOTH,
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
            scope=RuleScope.BOTH,
            category="test",
        )


def test_validate_catalog_raises_on_duplicate() -> None:
    """validate_catalog raises ValueError on duplicate IDs."""
    r1 = RuleDefinition(
        id="E001",
        name="R1",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        scope=RuleScope.BOTH,
        category="test",
    )
    r2 = RuleDefinition(
        id="E001",
        name="R2",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        scope=RuleScope.BOTH,
        category="test",
    )
    with pytest.raises(ValueError, match="duplicate rule ID"):
        validate_catalog([r1, r2])

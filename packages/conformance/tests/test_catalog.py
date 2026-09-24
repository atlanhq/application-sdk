"""Tests for the rule catalog and RuleDefinition model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conformance.suite.rules import CATALOG, _combine_rules, get_rule
from conformance.suite.schema import load_catalog
from conformance.suite.schema.catalog import RuleDefinition, validate_catalog
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)
from conformance.suite.schema.extensions import AtlanRuleProperties
from pydantic import ValidationError

import conformance


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


def test_catalog_block_rules_state_customer_impact() -> None:
    """Every BLOCK-tier rationale must state its customer failure mode.

    Tier is the criticality model (FND-221): block = customer risk, warn =
    good-to-have. That semantic only stays auditable if each BLOCK rule's
    rationale says concretely how the violation becomes a customer issue — a
    rule that cannot state one does not belong at BLOCK (FND-311).
    """
    rules = load_catalog()
    missing = [
        rule.id
        for rule in rules
        if rule.tier is EnforcementTier.BLOCK
        and "Customer impact:" not in rule.rationale
    ]
    assert not missing, (
        f"BLOCK rules whose rationale has no 'Customer impact:' line: {missing} — "
        "state how the violation turns into a customer issue, or keep the rule at WARN"
    )


#: Phrases that *argue for* the WARN tier, as opposed to merely mentioning it.
#: A BLOCK rule may legitimately say "hence BLOCK, not WARN" or "promoted from
#: warn to block" — that is a tier reference. These are justifications, and a
#: BLOCK rule carrying one publishes a doc page whose tier column and body
#: disagree (``gen-rule-docs`` renders both from the same definition).
#:
#: Word-boundary regexes, not bare substrings: ``"this is a warn"`` as a
#: substring also matches "this is a warn*ing* sign", which never argues for
#: the WARN tier. ``\b`` keeps the match on the standalone phrase. A boundary
#: is only added on a side where the phrase actually begins/ends with a word
#: char — anchoring ``\b`` against a leading/trailing backtick or paren would
#: force a word char that isn't there and the phrase would never match.
def _word_boundary(phrase: str) -> re.Pattern[str]:
    left = r"\b" if phrase[0].isalnum() else ""
    right = r"\b" if phrase[-1].isalnum() else ""
    return re.compile(left + re.escape(phrase) + right)


_WARN_JUSTIFYING_PHRASES = tuple(
    _word_boundary(phrase)
    for phrase in (
        "this is a warn",
        "land as ``warn``",
        "warn (not block)",
        "warn (new-rule tier policy)",
        "warn (per the new-rule tier policy)",
    )
)


_AUTOFIX_DENYING_PHRASES = tuple(
    _word_boundary(phrase)
    for phrase in (
        "not autofixable",
        "no autofix",
        "route to residue",
        "routes to residue",
        "rather than an autofix",
    )
)

#: Prose that claims a rule IS autofixable. The inverse blind spot: rewording a
#: batch of "NOT autofixable:" openings in one pass is easy to land on a rule
#: whose flag was False all along, producing the same contradiction upside down.
_AUTOFIX_CLAIMING_PHRASES = tuple(
    _word_boundary(phrase) for phrase in ("autofixable per-site", "is autofixable")
)


def _flat_prose(rule: RuleDefinition) -> str:
    """Rationale + description, lowercased, with all whitespace collapsed.

    Collapsing is load-bearing, not tidiness. These strings are hand-wrapped
    across source lines, so a phrase is routinely split by a newline mid-way
    ("findings route\nto residue"). Matching the raw text misses those silently
    — the same class of blind spot as matching source text rather than the
    concatenated literals, one level further down.
    """
    return re.sub(r"\s+", " ", f"{rule.rationale}\n{rule.full_description}").lower()


def test_autofixable_rules_do_not_deny_their_own_autofixability() -> None:
    """An ``autofixable = True`` rule's own prose must not say it is not.

    The fleet classification (FND-2477) set ``autofixable`` as a structured
    attribute across the catalog, but most of these paragraphs were written
    before it existed and still open with "NOT autofixable:" or "findings route
    to residue rather than an autofix". The generated doc renders the flag and
    the prose side by side, so the page contradicts itself — and the remediation
    lane reads the flag, so it picks the rule up regardless.

    That is not a cosmetic mismatch. The lane is driven entirely by this flag,
    so every one of these is a rule that tells an operator "act" and then tells
    the engineer reading the doc "this cannot be acted on mechanically". The
    honest form is to keep the flag (the rule IS in the auto-fixable lane) and
    say what the prose actually means: the fix is per-site human judgement, not
    a mechanical rewrite.

    Generalises the same failure mode as
    ``test_catalog_block_rules_carry_no_warn_justifying_prose``: an attribute
    was changed and the paragraph explaining the old value was left behind.
    """
    rules = load_catalog()
    offenders = [
        (rule.id, phrase.pattern)
        for rule in rules
        if rule.autofixable
        for phrase in _AUTOFIX_DENYING_PHRASES
        if phrase.search(_flat_prose(rule))
    ]
    assert not offenders, (
        "autofixable rules whose own prose denies it: "
        f"{offenders} — say the fix needs per-site judgement rather than that "
        "the rule is not autofixable, or set autofixable=False"
    )


def test_autofix_denying_phrases_do_not_over_match() -> None:
    """The guard must not fire on prose that merely mentions autofixing."""
    prose = "the autofix rewrites the call in place".lower()
    assert not any(p.search(prose) for p in _AUTOFIX_DENYING_PHRASES)
    canonical = (
        "not autofixable: orjson is not a drop-in replacement",
        "it stays advisory (warn, no autofix) because",
        "findings route to residue instead",
        "findings routes to residue instead",
        "so findings go rather than an autofix",
    )
    for phrase, sample in zip(_AUTOFIX_DENYING_PHRASES, canonical, strict=True):
        assert phrase.search(sample), f"{phrase.pattern!r} stopped matching {sample!r}"

    # A phrase wrapped across source lines must still match once flattened —
    # the case the guard missed on its first outing (P023).
    wrapped = re.sub(
        r"\s+", " ", "remediation is a restructure, so findings route\nto residue."
    )
    assert any(
        p.search(wrapped) for p in _AUTOFIX_DENYING_PHRASES
    ), "a newline-split phrase must match after whitespace collapse"


def test_non_autofixable_rules_do_not_claim_to_be_autofixable() -> None:
    """The same contradiction, upside down.

    A rule carrying ``autofixable=False`` whose prose opens "Autofixable
    per-site…" reads exactly as wrong on the generated page, and is the easier
    of the two to introduce: rewording a batch of "NOT autofixable:" openings in
    one pass lands on the rules whose flag was False all along. Both directions
    are the same defect — the flag and the paragraph disagreeing — so both are
    guarded.
    """
    offenders = [
        (rule.id, phrase.pattern)
        for rule in load_catalog()
        if not rule.autofixable
        for phrase in _AUTOFIX_CLAIMING_PHRASES
        if phrase.search(_flat_prose(rule))
    ]
    assert not offenders, (
        "non-autofixable rules whose prose claims otherwise: "
        f"{offenders} — say 'Not a mechanical rewrite: …' rather than "
        "'Autofixable per-site', or set autofixable=True"
    )


def test_catalog_block_rules_carry_no_warn_justifying_prose() -> None:
    """A BLOCK rule's own prose must not argue for WARN.

    Promotions are easy to do halfway: flip the tier and leave the paragraph
    that explains why the rule is only a warning. The generated doc renders
    tier and prose side by side, so the result is a page that contradicts
    itself — and nothing else catches it. P030 hit exactly this in FND-311;
    this generalises that rule-specific pin to the whole catalog.
    """
    rules = load_catalog()
    offenders = [
        (rule.id, phrase.pattern)
        for rule in rules
        if rule.tier is EnforcementTier.BLOCK
        for phrase in _WARN_JUSTIFYING_PHRASES
        if phrase.search(f"{rule.rationale}\n{rule.full_description}".lower())
    ]
    assert not offenders, (
        "BLOCK rules whose prose still argues for WARN: "
        f"{offenders} — rewrite the paragraph to say why the rule blocks, or "
        "return the rule to WARN"
    )


def test_warn_justifying_phrases_do_not_over_match() -> None:
    """Regression pin for the word-boundary fix.

    "This is a warning sign …" is ordinary English, not a WARN-tier
    justification — a bare substring match on ``"this is a warn"`` trips it.
    The word-boundary regexes must not.
    """
    prose = "This is a warning sign for operators".lower()
    assert not any(p.search(prose) for p in _WARN_JUSTIFYING_PHRASES)
    # Every real justifying phrase still matches its own canonical form — a
    # boundary fix that silences a true positive would gut the guard.
    canonical = (
        "this is a warn, not a block",
        "should land as ``warn`` here",
        "tier is warn (not block)",
        "tier is warn (new-rule tier policy)",
        "tier is warn (per the new-rule tier policy)",
    )
    for phrase, prose in zip(_WARN_JUSTIFYING_PHRASES, canonical):
        assert phrase.search(prose), f"{phrase.pattern!r} stopped matching {prose!r}"


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

    D011 (conformance suite undeclared) is app-scoped: the SDK *publishes* the
    package, so it has no reason to declare it as a consumer, and the rule would
    be pure noise there.

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
    # type-checking) the SDK publishes. D009: apps fetching Dapr components
    # from GitHub instead of the installed wheel — the SDK's own
    # download-components task never does this (it lists local files).
    # P004/P005: apps must reach the
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
    # P026: getattr-with-default on a typed entrypoint/task contract param —
    # only apps own the @entrypoint/@task methods that consume the contract
    # (BLDX-1501). P027: app_state used as a cross-task data channel — the SDK
    # defines get/set_app_state but apps are the ones that (mis)use it as a
    # conduit (BLDX-1500). P028: hand-built qualifiedName f-strings — connectors
    # mint asset qualifiedNames; the SDK is the framework, not an asset author
    # (BLDX-1499). P052: asset serialization that bypasses entity_bytes —
    # only apps map and write assets; the SDK owns the seam (FND-2725).
    # P025: app-name alignment — only apps have an atlan.yaml and .env.example;
    # the SDK has neither, so this check is meaningless there (BLDX-1491).
    # P029/P030 + P037/P038/P039/P042: SDR-readiness — only apps declare
    # self_deployed_runtime; the SDK itself never does, so these are APP-scoped.
    # F001–F004: preflight-gate authoring — only apps register @task activities,
    # define Handler.preflight_check, construct PreflightCheck results, and declare
    # the entrypoint Input contracts the gate rebuilds metadata from; the SDK
    # publishes the gate, it is not a subject of these rules (BLDX-1545).
    # T002/T003: SDR test-quality — apps that declare SDR must have an SDR test
    # class; the SDK itself is not an SDR app (DISTR-752).
    # T004: dev-entrypoint delegation — only consumer apps have a root main.py
    # that CI's connector-integration-tests action runs directly; the SDK has
    # no such file (BLDX-1520).
    # I001–I005: Dockerfile conformance (SDK builds the base image, not consuming it).
    # B001: consuming a deprecated SDK symbol (BLDX-1418).
    # O002/O003/O004: asset-mapper usage — connectors build assets with pyatlan_v9,
    # serialize with to_nested_bytes, and type their mapper returns (BLDX-1492); the
    # SDK is the framework, not a connector.
    # O006: direct rocksdict import — application_sdk.common.spillable_dict and
    # application_sdk.common.incremental.storage.rocksdb_utils are themselves the
    # intended callers of rocksdict; the SDK is the publisher of this seam, not a
    # consumer of it (CNCT-80, CNCT-191).
    # K001/K002: contract-toolkit conformance — only app repos have a contract/
    # directory with .pkl source files; the SDK has no contract/ dir to scan
    # (BLDX-1479).
    # K003/K004/K005: generated-artifact freshness — a stale Pkl lock, a missing
    # generated output, or a stripped provenance banner are all app-repo concerns
    # (the SDK has no contract/ + generated app artifacts) (BLDX-1414).
    # K006: manifest-vs-contract field validation — only app repos have a
    # generated app/generated/**/manifest.json DAG to cross-reference against a
    # Python Output contract; the SDK has no such generated artifact (BLDX-1527).
    # K007/K008: toolkit version floor + source provenance — the app's PklProject
    # declares the app-contract-toolkit dependency; the SDK *is* the publisher, so
    # it has no such dependency to grade (BLDX-1479). K009: unresolved scaffold
    # placeholder in a generated artifact; K010: missing generated E2E scaffolding
    # — both are app-repo generated-output concerns (BLDX-1479).
    # K011/K012: release-readiness — the generated atlan.yaml's app_id and the
    # pyproject generate poe task only exist on a consumer app that publishes to
    # the marketplace; the SDK has no contract/ dir, no atlan.yaml, and no
    # marketplace publish, so neither rule applies to it (CONNECT release-pipeline).
    # K014: same release-readiness family — release_model selects how the app
    # reaches tenants, and only a consumer app has an atlan.yaml declaring it.
    # K015: legacy_workflow_types agreement — the rule compares a consumer app's
    # generated manifest against its App subclass; the SDK declares neither
    # (CONNECT-1081).
    # K016: undeclared artifact on an entry-point boundary — artifactSchemas is
    # authored in an app's pkl contract and rendered into its app/generated/
    # tree; the SDK ships neither, and the hand-offs the rule protects are
    # between apps, not inside the framework (ADR-0020).
    # K017: a declared artifact schema contradicted by the app's own writer —
    # same generated-tree + app-Python pairing as K016, neither of which the SDK
    # has (ADR-0020).
    # K018/K019/K020/K021: inbound-config guards over an app's generated
    # manifest and its entrypoint Input contract — an undeclared extract arg
    # (K018), an unwired uiConfig form key (K019), a legacy args.metadata
    # envelope (K020), and a filter field typed as a strict dict that rejects the
    # AE's flat JSON string (K021, CONNECT-1333 / CONNECT-1389). All four need an
    # app's contract/ + app/generated/ tree, which the SDK does not have.
    # E020: HTTP-failure-to-empty-return — the harm (publishing a partial crawl as
    # complete) is a connector extract/publish concern; the SDK's matching sites are
    # legitimate best-effort infra (health/metric scrapes), not crawlers (BLDX-1503).
    # S002: raw-env credential reads — the SDK is the *provider* of the secret-store
    # seam (EnvironmentSecretStore legitimately reads os.environ), so the rule that
    # steers apps onto that seam is meaningless on the SDK itself (BLDX-1419). S001
    # (hardcoded credentials) stays 'both'.
    # T010/T011/T012: missing unit/integration/e2e test suite — these encode the
    # agreed per-connector testing-tier architecture (unit+integration required,
    # e2e recommended); the SDK's own tests/ layout is graded by its own coverage
    # gate (fail_under=85), not this per-app tiering policy (BLDX-1400).
    # T014/T015: coverage-config integrity (disabled fail_under gate, omit/source
    # hiding app/ product code) — only connector apps have an app/ product-code
    # tree with a ratcheting coverage floor; the SDK's own coverage config is a
    # different, already-enforced policy (BLDX-1400).
    # T016: e2e CI compose overlay must inherit ATLAN_DEPLOYMENT_NAME — only
    # connector apps ship a .github/e2e/ docker-compose overlay for the full-DAG
    # worker; the SDK has no such overlay to grade (the sdr-e2e action that
    # derives the per-leg value lives here, but it is not a compose overlay).
    # T017: e2e agent_spec() override must inherit the per-leg deployment queue —
    # only connector apps subclass the e2e harness and (may) override agent_spec;
    # the SDK ships the env-derived default, it doesn't hard-code a connector queue.
    # T020-T022: full-DAG e2e CI wiring — only connector apps call
    # tests-reusable.yaml / the sdr-e2e action, ship tests/e2e/ suites the reusable
    # discovers, and declare self_deployed_runtime in atlan.yaml. The SDK *is* the
    # publisher of the reusable and the action, so none of the three grade it.
    # T023/T024: e2e harness scaffold + run mode — only connector apps have a
    # contract/app.pkl the toolkit generates _e2e_base/_e2e_credential/
    # _e2e_substitutions from, and only they subclass the harness the SDK ships.
    # B007: daft-only DataFrame APIs on SDK reader frames — only consumer apps
    # call daft surfaces on frames the SDK hands them; the SDK's own transformer
    # code is the pyarrow/pandas bridge itself (fleet SDR sweep).
    # D010: query-transformer-without-duckdb — the app's lock must resolve
    # duckdb; the SDK is the publisher of the [sql]/[incremental] extras.
    # P040: transform-template reserved keywords — only connector apps ship
    # transform YAML templates consumed by the query transformer.
    # P042: hand-rolled upload_to_atlan bridge in an SDR app — same gating as
    # P030, which it was split out of.
    # P051: SDR interactive-setup SDK floor — only apps declare
    # self_deployed_runtime and lock a consumed application-sdk version; the SDK
    # itself is neither an SDR app nor a consumer of its own wheel (DISTR-752).
    # P043/P045: error-seam — apps must build control flow on the SDK's public
    # error surface (application_sdk.errors.__all__), not on an internal error
    # class that can move, or stop being the one a boundary raises, in a minor
    # release. The SDK is the publisher of that surface, so neither rule grades
    # it (CONNECT-970).
    assert app_scoped == {
        "B001",
        "B007",
        "B008",
        "D010",
        "P040",
        "P042",
        "P043",
        "P044",
        "P045",
        "F005",
        "P048",
        "P049",
        "P051",
        "P052",
        "C002",
        "D001",
        "D002",
        "D004",
        "D005",
        "D006",
        "D007",
        "D008",
        "D009",
        "D011",
        "E020",
        "K001",
        "K002",
        "K003",
        "K004",
        "K005",
        "K006",
        "K007",
        "K008",
        "K009",
        "K010",
        "K011",
        "K012",
        "K013",
        "K014",
        "K015",
        "K016",
        "K017",
        "K018",
        "K019",
        "K020",
        "K021",
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
        "P026",
        "P027",
        "P028",
        "P029",
        "P030",
        "F001",
        "F002",
        "F003",
        "F004",
        "P037",
        "P038",
        "P039",
        "T002",
        "T003",
        "T004",
        "T010",
        "T011",
        "T012",
        "T014",
        "T015",
        "T016",
        "T017",
        "T018",
        "T020",
        "T021",
        "T022",
        "T023",
        "T024",
        "T025",
        "O002",
        "O003",
        "O004",
        "O006",
        "I001",
        "I002",
        "I003",
        "I004",
        "I005",
        "S002",
        "F006",
        "F007",
        "F008",
        "F009",
        "F010",
        "F011",
        "F012",
        "F013",
        "F014",
        "F015",
        "F016",
        "F019",
        "F020",
    }, app_scoped
    # SDK-only rules: the SDK must keep Temporal contained behind its seam
    # (P006/P007, BLDX-1417), declare its deprecations correctly (B002–B004),
    # keep its text file IO locale-independent (P046) — the SDK repo is the
    # only one in the fleet that runs a Windows CI leg, where the platform
    # default codec is cp1252 rather than UTF-8 — and publish destination
    # files atomically (P050, CONNECT-1126): the transfer and writer seams
    # live in the SDK, so the in-place-write hazard does too.
    sdk_scoped = {r.id for r in rules if r.scope == RuleScope.SDK}
    assert sdk_scoped == {
        "B002",
        "B003",
        "B004",
        "P006",
        "P007",
        "P046",
        "P050",
        "F017",
        "F018",
    }, sdk_scoped
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
        "E019",
        "E020",
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
    expected = {"C001", "C002", "C003", "C004"}
    missing = expected - c_ids
    assert not missing, f"Missing C-series rules: {missing}"


def test_catalog_d_series_present() -> None:
    """The D-series dependency rules are all present."""
    rules = load_catalog()
    d_ids = {r.id for r in rules if r.id.startswith("D")}
    expected = {
        "D001",
        "D002",
        "D003",
        "D004",
        "D005",
        "D006",
        "D007",
        "D008",
        "D009",
        "D010",
    }
    missing = expected - d_ids
    assert not missing, f"Missing D-series rules: {missing}"


def test_catalog_p_series_present() -> None:
    """The P-series prescription rules are exactly P001–P025, P031.

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
    P031 is SharedDefaultExecutorOffload — asyncio.to_thread(...) /
    run_in_executor(None, ...) bypass the SDK's dedicated run_in_thread() pool
    and land on asyncio's shared default executor instead (BLDX-1525).
    P032–P035 and P047 are retired: the preflight-gate rules moved to the
    F-series as F001–F005 (PR #3710) and those P-ids stay vacant.
    P036 is HandRolledProcessIsolation — a bare ProcessPoolExecutor /
    multiprocessing child instead of the run_fault_isolated() / run_best_effort()
    seam (CNCT-85).
    P037 is SdrAgentJsonNotConsumed (credentials resolved by GUID only, agent_json
    ignored), P038 is SdrArtifactMisrooted (object-store prefix rooted from an
    empty-defaulting input field), and P039 is SdrAgentJsonDroppedByInputContract
    (the generated extract-input contract silently drops the forwarded agent_json)
    — the follow-on SDR-readiness rules.
    P040 is TransformTemplateReservedKeyword — an unquoted DuckDB reserved
    keyword used as an identifier in a transform SQL template (ParserException
    at runtime on the daft-less SDK >= 3.22 runtime; fleet SDR sweep).
    P042 is SdrHandRolledUploadBridge — a working custom upload_to_atlan
    standing in for App.upload(), split out of P030 so the "bytes move but the
    SDK contract is reimplemented" shape carries its own severity, its own
    remediation, and a retirement date (the v4.0 removal of upload_to_atlan).
    P043/P045 are the error-seam rules — NonPublicErrorControlFlow and
    PrivateErrorClassImport. Only ``application_sdk.errors.__all__`` is the
    public error contract; an ``except`` on an internal class silently stops
    matching when the SDK changes which class a boundary surfaces, because the
    replacement is a sibling rather than a subclass (CONNECT-970).
    P046 is LocaleDependentTextIO — Path.read_text()/write_text() and a
    text-mode open() with no encoding= decode using the locale's codec, which is
    cp1252 on the Windows legs of the SDK's unit matrix and UTF-8 everywhere
    else (FND-924).
    P051 is SdrPreflightUnavailable — an SDR app whose uv.lock resolves
    application-sdk below 3.30.0, the floor at which the interactive setup
    surfaces (test auth / preflight / metadata browsing) become available; a WARN
    readiness nudge, not a data-loss bug (DISTR-752).
    P052 is EntitySerializationBypass — app code serializing a pyatlan asset
    itself (to_nested_bytes / to_nested_dict / pyatlan_v9 to_atlas_format)
    instead of through the SDK's entity_bytes seam (FND-2725).
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
        "P026",
        "P027",
        "P028",
        "P029",
        "P030",
        "P031",
        "P036",
        "P037",
        "P038",
        "P039",
        "P040",
        "P042",
        "P043",
        "P044",
        "P045",
        "P046",
        "P048",
        "P049",
        "P050",
        "P051",
        "P052",
    }
    missing = expected - p_ids
    assert not missing, f"Missing P-series rules: {missing}"
    extra = p_ids - expected
    assert not extra, f"Unexpected P-series rules: {extra}"


def test_catalog_f_series_present() -> None:
    """The F-series preflight-gate rules are exactly F001–F020.

    F001–F005 were published as P032–P035 and P047 and moved to their own
    series in PR #3710 before any fleet suppression referenced them; the vacated
    P-ids are retired and never reused.  F006–F019 are the CONNECT-812 contract,
    lifetime and behavioral rules; F016–F018 are the opt-in TEST rules.  F020
    flags a suppression that still cites one of the five retired P-ids.

    There is deliberately no rule for a ``PreflightStatus.PARTIAL`` verdict: it
    is a read of a deprecated SDK enum member, which B001 already reports
    fleet-wide from the deprecated-symbol manifest, and two WARN findings on one
    line is worse than one.
    """
    f_ids = {r.id for r in load_catalog() if r.id.startswith("F")}
    expected = {f"F{n:03}" for n in range(1, 21)}
    assert f_ids == expected, f"F-series drift: {sorted(f_ids ^ expected)}"


def test_catalog_o_series_present() -> None:
    """The O-series optimisation rules are all present."""
    rules = load_catalog()
    o_ids = {r.id for r in rules if r.id.startswith("O")}
    expected = {"O001", "O002", "O003", "O004", "O005", "O006"}
    missing = expected - o_ids
    assert not missing, f"Missing O-series rules: {missing}"


def test_catalog_t_series_present() -> None:
    """The T-series test-quality rules are all present: T001 (integration
    marking), T002/T003 (SDR test-quality), T004 (dev-entrypoint), T005-T009
    (assertion/collection quality), T010-T013 (tier structure), T014/T015
    (coverage-config), T016/T017 (e2e-CI queue isolation), T018
    (integration tier deselected by addopts), T019 (asyncio test-loop scope
    unset relative to a broadened fixture loop scope), T020-T022 (full-DAG e2e
    must run through the reusable Tests workflow: no bespoke sdr-e2e workflow,
    suites reachable in CI, two-store posture on SDR apps), and T023/T024 (e2e
    harness scaffold generated from contract/app.pkl; RunMode declared), and T025
    (every bundle entrypoint has an e2e suite, not just the default one)."""
    rules = load_catalog()
    t_ids = {r.id for r in rules if r.id.startswith("T")}
    expected = {f"T{n:03d}" for n in range(1, 26)}
    missing = expected - t_ids
    assert not missing, f"Missing T-series rules: {missing}"
    extra = t_ids - expected
    assert not extra, f"Unexpected T-series rules: {extra}"


def test_catalog_b_series_present() -> None:
    """The B-series backwards-compatibility / deprecation rules are all present.

    B007 is DaftOnlyDataframeApiUsage — daft-only DataFrame APIs
    (count_rows/to_pylist/.names) that are dead on the daft-less SDK >= 3.22
    runtime; third-party surfaces the generated deprecated-symbol manifest
    cannot carry (fleet SDR sweep).  ``DataframeType.daft`` is the SDK's own
    symbol, so it rides the generated manifest and B001 reports it — the
    ownership split the B007 rule definition and remediation prose describe.
    """
    rules = load_catalog()
    b_ids = {r.id for r in rules if r.id.startswith("B")}
    expected = {"B001", "B002", "B003", "B004", "B005", "B006", "B007", "B008"}
    missing = expected - b_ids
    assert not missing, f"Missing B-series rules: {missing}"
    extra = b_ids - expected
    assert not extra, f"Unexpected B-series rules: {extra}"


def test_catalog_k_series_present() -> None:
    """The K-series contract-toolkit rules are K001/K002 (source), the
    generated-artifact freshness rules K003/K004/K005 (BLDX-1414), the
    manifest-vs-contract field validation rule K006 (BLDX-1527), the toolkit
    hygiene rules K007–K010 (version floor, source provenance, unresolved
    placeholder, missing E2E scaffolding) (BLDX-1479), the release-readiness
    guards K011/K012 (atlan.yaml app_id, generate poe task), the DAG-node
    log-identity guard K013 (toolkit-owned workflow filed under
    ``automation-engine``) (CNCT-24), the release-model declaration guard K014,
    and the legacy-alias agreement rule K015 (manifest legacy_workflow_types vs
    the SDK App declaration) (CONNECT-1081), plus the artifact-schema pair K016
    (a public hand-off with no declaration) and K017 (a declaration its own
    writer contradicts) (ADR-0020)."""
    rules = load_catalog()
    k_ids = {r.id for r in rules if r.id.startswith("K")}
    expected = {
        "K001",
        "K002",
        "K003",
        "K004",
        "K005",
        "K006",
        "K007",
        "K008",
        "K009",
        "K010",
        "K011",
        "K012",
        "K013",
        "K014",
        "K015",
        "K016",
        "K017",
        "K018",
        "K019",
        "K020",
        "K021",
    }
    missing = expected - k_ids
    assert not missing, f"Missing K-series rules: {missing}"
    extra = k_ids - expected
    assert not extra, f"Unexpected K-series rules: {extra}"


def test_catalog_s_series_present() -> None:
    """The S-series secret-hygiene rules are exactly S001 and S002."""
    rules = load_catalog()
    s_ids = {r.id for r in rules if r.id.startswith("S")}
    expected = {"S001", "S002"}
    missing = expected - s_ids
    assert not missing, f"Missing S-series rules: {missing}"
    extra = s_ids - expected
    assert not extra, f"Unexpected S-series rules: {extra}"


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
    assert descriptor.properties["atlan/autofixable"] is True
    assert descriptor.properties["atlan/orthogonalGate"] == "tests"
    # The reference-app pointer rides the wire so a remediation model reading
    # only the SARIF knows which file to open before proposing a fix.
    assert descriptor.properties["atlan/canonicalReference"] == p001.canonical_reference
    assert "atlan-" in descriptor.properties["atlan/canonicalReference"]


def test_to_reporting_descriptor_roundtrip_forces_external_influence() -> None:
    """C001's forces_external_influence=True survives the SARIF round-trip,
    and a rule that doesn't set it (E001) omits the property entirely --
    the field is only ever emitted when True (see AtlanRuleProperties.to_properties)."""
    c001 = get_rule("C001")
    descriptor = c001.to_reporting_descriptor()
    assert descriptor.properties["atlan/forcesExternalInfluence"] is True

    e001 = get_rule("E001")
    descriptor = e001.to_reporting_descriptor()
    assert "atlan/forcesExternalInfluence" not in descriptor.properties


def test_atlan_rule_properties_forces_external_influence_roundtrip() -> None:
    """to_properties() -> from_properties() preserves forces_external_influence
    in both directions, so a typo in the ``atlan/forcesExternalInfluence`` key
    on either side would fail this test rather than silently defeating C001's
    mandatory-human-review guarantee."""
    props = AtlanRuleProperties(
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="ci-supply-chain",
        forces_external_influence=True,
    )
    serialised = props.to_properties()
    assert serialised["atlan/forcesExternalInfluence"] is True
    assert (
        AtlanRuleProperties.from_properties(serialised).forces_external_influence
        is True
    )

    default_props = AtlanRuleProperties(
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="ci-supply-chain",
    )
    default_serialised = default_props.to_properties()
    assert "atlan/forcesExternalInfluence" not in default_serialised
    assert (
        AtlanRuleProperties.from_properties(
            default_serialised
        ).forces_external_influence
        is False
    )


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


# ── Rule retirement: until / superseded_by ──────────────────────────────────


def _rule(**overrides) -> RuleDefinition:
    """A minimal valid rule, for exercising the retirement fields."""
    return RuleDefinition(
        **{
            "id": "E001",
            "name": "R1",
            "tier": EnforcementTier.WARN,
            "mechanism": RuleMechanism.STATIC,
            "scope": RuleScope.BOTH,
            "category": "test",
            **overrides,
        },
    )


def test_superseded_by_accepts_a_rule_id() -> None:
    assert _rule(superseded_by="P042").superseded_by == "P042"


def test_superseded_by_accepts_an_sdk_marker() -> None:
    assert _rule(superseded_by="sdk>=3.27.0").superseded_by == "sdk>=3.27.0"


@pytest.mark.parametrize(
    "value",
    [
        "P42",  # malformed rule ID
        "sdk >= 3.27.0",  # spaces
        "sdk>3.27.0",  # wrong operator
        "the daft fix",  # free text
        "4.0.0",  # bare version, ambiguous with `until`
    ],
)
def test_superseded_by_rejects_unactionable_markers(value: str) -> None:
    """Free text here would be silently ignored by every reader."""
    with pytest.raises(ValidationError, match="superseded_by"):
        _rule(superseded_by=value)


def test_superseded_by_cannot_name_the_rule_itself() -> None:
    with pytest.raises(ValidationError, match="itself"):
        _rule(id="P042", superseded_by="P042")


def test_retirement_fields_default_to_none() -> None:
    """Indefinite enforcement stays the default — retirement is opt-in."""
    rule = _rule()
    assert rule.until is None
    assert rule.superseded_by is None


def test_retirement_fields_reach_sarif_properties() -> None:
    props = _rule(
        since="0.18.0", until="0.30.0", superseded_by="sdk>=4.0.0"
    ).to_reporting_descriptor()
    assert props.properties["atlan/until"] == "0.30.0"
    assert props.properties["atlan/supersededBy"] == "sdk>=4.0.0"
    roundtripped = AtlanRuleProperties.from_properties(props.properties)
    assert roundtripped.until == "0.30.0"
    assert roundtripped.superseded_by == "sdk>=4.0.0"


def test_retirement_properties_absent_when_unset() -> None:
    """No keys at all for the common case, so reports stay readable."""
    props = _rule().to_reporting_descriptor().properties
    assert "atlan/until" not in props
    assert "atlan/supersededBy" not in props


def test_catalog_until_never_precedes_since() -> None:
    """A rule cannot retire before it was introduced.

    Checked here rather than in the model so the schema layer stays free of
    upward imports to the check layer's version helpers.
    """
    from conformance.suite.checks._version import parse_version

    for rule in load_catalog():
        if rule.until is None or rule.since is None:
            continue
        until, since = parse_version(rule.until), parse_version(rule.since)
        assert (
            until is not None and since is not None
        ), f"{rule.id}: since/until must be parseable versions"
        assert (
            until >= since
        ), f"{rule.id}: until {rule.until} precedes since {rule.since}"


def test_catalog_retired_rules_are_removed() -> None:
    """The forcing function: a rule past its ``until`` must no longer ship.

    ``since`` alone gives an interim net no way out — it becomes permanent by
    construction. This is what makes ``until`` a commitment rather than a
    comment: once the package version reaches it, this test fails until the
    rule is actually deleted, the same way the deprecation drift gate fails on
    a stale manifest.
    """
    from conformance.suite.checks._version import parse_version, version_reached

    from conformance import __version__

    current = parse_version(__version__)
    assert current is not None, f"unparseable package version {__version__!r}"

    overdue = [
        f"{rule.id} (until {rule.until})"
        for rule in load_catalog()
        if rule.until is not None
        and (parsed := parse_version(rule.until)) is not None
        and version_reached(parsed, current)
    ]
    assert not overdue, (
        f"Rules past their retirement version at {__version__}: {overdue}. "
        "Delete the rule and its checker, or move `until` out with a recorded "
        "reason."
    )


def test_catalog_superseding_rule_ids_exist() -> None:
    """A ``superseded_by`` rule ID must name a rule that is actually in the catalog."""
    rules = load_catalog()
    known = {rule.id for rule in rules}
    dangling = [
        f"{rule.id} -> {rule.superseded_by}"
        for rule in rules
        if rule.superseded_by is not None
        and not rule.superseded_by.startswith("sdk>=")
        and rule.superseded_by not in known
    ]
    assert not dangling, f"superseded_by names an unknown rule: {dangling}"


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


# ── orthogonal_gate wiring ────────────────────────────────────────────────
#
# A gate name is only useful if something implements it. Declaring
# ``orthogonal_gate="docker-buidl"`` on a rule, or adding a new gate name without
# a matching prose contract, otherwise fails *silently at remediation time*:
# ``orthogonal-gate.prose.md`` fails closed on an unknown value, so every fix for
# that rule reverts and residues with nothing to distinguish it from a genuinely
# un-fixable finding. These tests move that failure to CI.


def _gate_dispatch_prose() -> str:
    from importlib.resources import files

    return (
        files("conformance")
        .joinpath("programs/functions/orthogonal-gate.prose.md")
        .read_text()
    )


def test_every_declared_gate_is_dispatched_by_the_prose() -> None:
    """Each distinct orthogonal_gate value appears in the dispatch contract."""
    dispatch = _gate_dispatch_prose()
    declared = {r.orthogonal_gate for r in load_catalog() if r.orthogonal_gate}
    missing = sorted(g for g in declared if f'"{g}"' not in dispatch)
    assert not missing, (
        f"orthogonal_gate value(s) {missing} are declared on rules but never "
        "dispatched in programs/functions/orthogonal-gate.prose.md — the "
        "dispatcher fails closed, so every fix for those rules would revert"
    )


def test_delegating_gates_have_a_prose_contract() -> None:
    """A gate that delegates has a functions/<gate>-gate.prose.md to delegate to."""
    from importlib.resources import files

    # "tests" and "skip" are handled inline by the dispatcher; the rest delegate.
    inline = {"tests", "skip"}
    declared = {r.orthogonal_gate for r in load_catalog() if r.orthogonal_gate}
    for gate in sorted(declared - inline):
        contract = files("conformance").joinpath(
            f"programs/functions/{gate}-gate.prose.md"
        )
        assert contract.is_file(), (
            f"orthogonal_gate={gate!r} delegates, but "
            f"programs/functions/{gate}-gate.prose.md does not exist"
        )


def test_i_series_uses_the_docker_build_gate() -> None:
    """Every I-series rule is gated by an actual image build.

    The dockerfile area was propose-only precisely because no gate validated a
    Dockerfile change: ``"tests"`` is blind there (a Dockerfile edit cannot move
    the Python suite) and ``"skip"``'s parse check has no Dockerfile parser. If any
    I rule loses this gate, the area silently returns to accepting unverified
    fixes under ``--apply-unverifiable``.
    """
    i_rules = [r for r in load_catalog() if r.id.startswith("I")]
    assert i_rules, "no I-series rules found — the guard below would be vacuous"
    wrong = {
        r.id: r.orthogonal_gate for r in i_rules if r.orthogonal_gate != "docker-build"
    }
    assert not wrong, f"I-series rules not gated by docker-build: {wrong}"


def test_docker_build_is_accepted_by_the_model() -> None:
    """The Literal admits the gate name, and a typo fails at definition time."""
    rule = RuleDefinition(
        id="I999",
        name="Probe",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        scope=RuleScope.APP,
        category="dockerfile-probe",
        orthogonal_gate="docker-build",
    )
    assert rule.orthogonal_gate == "docker-build"
    props = rule.to_reporting_descriptor().properties
    assert props["atlan/orthogonalGate"] == "docker-build"

    with pytest.raises(ValidationError):
        RuleDefinition(
            id="I998",
            name="Typo",
            tier=EnforcementTier.BLOCK,
            mechanism=RuleMechanism.STATIC,
            scope=RuleScope.APP,
            category="dockerfile-probe",
            orthogonal_gate="docker-buidl",
        )


# ── fix_locus and the guidance fields ────────────────────────────────────────


def test_redundant_fix_locus_is_rejected() -> None:
    """A locus that only restates ``scope`` cannot be constructed.

    ``scope`` already says which repos a rule runs against, and the fix normally
    lands in that repo's own source.  Spelling that out again put a token on
    half the catalog that told a reader nothing — and a field that is usually
    noise stops being read on the occasions it matters, which is exactly when
    the fix is in the toolkit or the contract.  Rejecting it in the model rather
    than here means it cannot creep back one rule at a time.
    """
    for scope, locus in (
        (RuleScope.APP, FixLocus.APP),
        (RuleScope.BOTH, FixLocus.APP),
        (RuleScope.SDK, FixLocus.SDK),
    ):
        with pytest.raises(ValidationError, match="only restates scope"):
            RuleDefinition(
                id="E999",
                name="RedundantLocus",
                tier=EnforcementTier.WARN,
                mechanism=RuleMechanism.STATIC,
                scope=scope,
                category="test",
                fix_locus=locus,
            )


def test_fix_locus_is_optional_and_defaults_to_none() -> None:
    """Unset is the normal case, and reads as "the repo under scan".

    That is also the more accurate default for a ``both``-scoped rule: a literal
    ``app`` would have been wrong every time the rule fired on the SDK.
    """
    rule = RuleDefinition(
        id="E999",
        name="NoLocus",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        scope=RuleScope.BOTH,
        category="test",
    )
    assert rule.fix_locus is None


def test_declared_loci_are_surprising_ones() -> None:
    """Every locus the catalog does declare points away from the obvious place.

    The point of keeping the field at all is the minority of rules where an app
    engineer reading the finding would look in the wrong file.  This asserts the
    catalog holds that shape rather than drifting back to a per-rule restatement
    of ``scope``.
    """
    declared = {r.id: r.fix_locus for r in load_catalog() if r.fix_locus is not None}
    assert (
        declared
    ), "the informative loci (contract/toolkit/packaging/ci/tests) are gone"
    obvious = {
        r.id
        for r in load_catalog()
        if r.fix_locus is not None
        and r.fix_locus is (FixLocus.SDK if r.scope is RuleScope.SDK else FixLocus.APP)
    }
    assert not obvious, f"fix_locus restates scope on: {sorted(obvious)}"


def test_non_app_loci_explain_themselves() -> None:
    """A BLOCK rule the app cannot fix alone must say what to do instead.

    A ``contract``, ``toolkit`` or ``sdk`` locus means an app engineer reading
    the finding will look in the wrong place. A blocking finding with no route
    to a fix is what stalls a remediation lane indefinitely, so those rules have
    to carry at least one of ``canonical_reference`` / ``rule_interactions`` /
    ``terminal_state``.
    """
    needs_help = {FixLocus.TOOLKIT, FixLocus.SDK, FixLocus.CONTRACT}
    silent = [
        r.id
        for r in load_catalog()
        if r.fix_locus in needs_help
        and r.tier is EnforcementTier.BLOCK
        and not (r.canonical_reference or r.rule_interactions or r.terminal_state)
    ]
    assert not silent, (
        "BLOCK rules whose fix is not in the app must carry guidance "
        f"(canonical_reference / rule_interactions / terminal_state): {silent}"
    )


def test_rules_citing_a_suppression_as_compliant_license_it() -> None:
    """A rule whose compliant example IS a suppression must say so in ``terminal_state``.

    ``canonical_reference`` answers "what does correct look like here". When
    that answer is an inline ``ignore[<ID>]``, the rule is stating that a
    justified directive is the end state — but only ``terminal_state`` licenses
    one. A remediation lane reads ``terminal_state``, finds nothing, strips the
    directive and either re-opens settled work every cycle or applies a default
    edit the reference app deliberately rejected.

    E020 was exactly this: its reference named seven justified suppressions in
    ``atlan-metabase-app`` as the compliant example while declaring no
    ``terminal_state`` (FND-2547).
    """
    # Only a suppression of the rule's OWN id is a carve-out that needs a
    # licence. A directive for a different rule is just a site the reference
    # happens to show — F020 (directive hygiene) cites well-formed
    # ``ignore[P028]`` directives precisely as its compliant shape, and that says nothing
    # about when F020 itself may be suppressed.
    cites_suppression = re.compile(r"ignore\[([A-Z]\d+)\]")
    unlicensed = [
        r.id
        for r in load_catalog()
        if r.canonical_reference
        and r.id in cites_suppression.findall(r.canonical_reference)
        and not r.terminal_state
    ]
    assert not unlicensed, (
        "these rules name an inline suppression as their compliant example but "
        "declare no terminal_state to license it, so a remediation run cannot "
        f"tell a deliberate carve-out from an unfixed violation: {unlicensed}"
    )


#: The only repos a canonical reference may name.  Three maintained reference
#: apps (``docs/agents/canonical-apps.md``) plus the SDK itself for rules about
#: SDK-owned surfaces.  ``atlan-hello-world-app`` is deliberately absent: it is
#: the scaffold, too minimal to be what a fix is mirrored from (owner decision,
#: FND-2477).  An arbitrary connector is excluded on purpose: at any time some
#: are mid-migration and some carry patterns the SDK has deprecated, so copying
#: from one reproduces the fleet's median staleness.
_REFERENCE_REPOS = (
    "atlan-openapi-app",
    "atlan-mysql-app",
    "atlan-metabase-app",
    "application_sdk",
)

#: A reference has to point at something a reader can open.  Any of these is
#: evidence the value names a file or directory rather than describing one: a
#: slash-separated path (``app/handler.py``, ``tests/unit/``, ``contract/PklProject``),
#: a bare filename with a known extension, or one of the extensionless files a
#: repo root carries.  The bar is deliberately low — it only rules out prose.
_PATH_SHAPED = re.compile(
    r"(?:[\w.\-]+/[\w.\-]*)"
    r"|(?:\b[\w\-]+\.(?:py|pkl|ya?ml|json|toml|sql|lock|cfg|txt)\b)"
    r"|(?:\bDockerfile\b)"
    r"|(?:\.gitignore\b)"
)


def test_app_facing_rules_name_a_canonical_reference() -> None:
    """Every rule an app engineer can act on says what correct looks like.

    ``app``- and ``both``-scoped rules are the ones that reach a consumer repo.
    For those, "where is a version of this that is already right?" is the
    question the finding text cannot answer and the one that decides whether a
    fix converges or churns — so it is answered per rule, from a file in a
    maintained reference app, not left to whoever picks the finding up.
    """
    missing = [
        r.id
        for r in load_catalog()
        if r.scope in (RuleScope.APP, RuleScope.BOTH) and not r.canonical_reference
    ]
    assert not missing, (
        "app-facing rules with no canonical_reference — name a file in one of "
        f"{list(_REFERENCE_REPOS)} that already has the compliant shape: {missing}"
    )


def test_canonical_references_name_something_checkable() -> None:
    """A canonical reference has to be verifiable.

    Free-text encouragement is worse than nothing here, because it gets trusted.
    Require both a maintained reference repo *and* a path-shaped token, so a
    reader can open the file rather than infer which one was meant — a bare
    ``"atlan-mysql-app does this right"`` passes a substring check while
    answering nothing.
    """
    vague = [
        r.id
        for r in load_catalog()
        if r.canonical_reference
        and not (
            any(repo in r.canonical_reference for repo in _REFERENCE_REPOS)
            and _PATH_SHAPED.search(r.canonical_reference)
        )
    ]
    assert not vague, (
        "canonical_reference must name a reference repo AND a concrete path: "
        f"{vague}"
    )


#: The maintained reference apps alone — ``_REFERENCE_REPOS`` minus the SDK.
_REFERENCE_APPS = tuple(repo for repo in _REFERENCE_REPOS if repo != "application_sdk")

#: Auto-fixable rules allowed an SDK-only reference, each with the reason no
#: reference app can supply one. An entry is a claim about all three apps, so
#: it must be re-checked (and removed) the moment an app gains a real site.
_SDK_ONLY_REFERENCE_EXEMPT = {
    "E008": (
        "no reference app has an `except ImportError` in the code E008 scans "
        "(app/, main.py — tests/ is excluded); verified FND-2702"
    ),
    "T025": (
        "no reference app is in bundle mode (each emits a single generated "
        "manifest), so T025 inspects none of them; the positive shape is the "
        "SDK e2e harness until a multi-mode reference app exists (FND-2702)"
    ),
}

#: A positive citation: a reference-app name immediately followed by a path in
#: it (``atlan-mysql-app app/handler.py``, ``atlan-openapi-app pyproject.toml``).
#: A bare mention does not count — T025 once named all three apps only to say
#: none of them has the shape, and a substring check accepted that.
_POSITIVE_APP_CITATION = re.compile(
    r"(?:" + "|".join(re.escape(app) for app in _REFERENCE_APPS) + r")\s+"
    r"(?:[\w.\-]+/[\w.\-/]*"
    r"|[\w.\-]+\.(?:py|pkl|ya?ml|json|toml|sql|lock|cfg|txt|md)\b"
    r"|Dockerfile\b|\.gitignore\b)"
)


def test_autofixable_app_facing_rules_cite_a_reference_app() -> None:
    """An auto-fixable rule's fix is mirrored from an app, so it must cite one.

    ``test_canonical_references_name_something_checkable`` accepts
    ``application_sdk`` as a reference repo, which is right for a rule whose fix
    is an SDK seam — but an auto-fixable rule is applied by the remediation lane
    by mirroring how a reference app already does it, and an SDK-only reference
    gives the lane nothing to mirror. L012 and P003 both passed the substring
    check this way while citing no app at all (FND-2702). The citation must be
    positive — an app name followed by a path in it — so naming the apps as
    counter-examples does not satisfy it.
    """
    uncited = [
        r.id
        for r in load_catalog()
        if r.autofixable
        and r.scope in (RuleScope.APP, RuleScope.BOTH)
        and r.canonical_reference
        and not _POSITIVE_APP_CITATION.search(r.canonical_reference)
        and r.id not in _SDK_ONLY_REFERENCE_EXEMPT
    ]
    assert not uncited, (
        "auto-fixable app-facing rules whose canonical_reference cites no file "
        f"in a reference app — cite one in {list(_REFERENCE_APPS)}: {uncited}"
    )
    catalog = {r.id: r for r in load_catalog()}
    stale = [
        rule_id
        for rule_id in _SDK_ONLY_REFERENCE_EXEMPT
        if rule_id not in catalog
        or not catalog[rule_id].autofixable
        or _POSITIVE_APP_CITATION.search(catalog[rule_id].canonical_reference)
    ]
    assert (
        not stale
    ), f"_SDK_ONLY_REFERENCE_EXEMPT entries no longer needed — remove them: {stale}"


#: A count of things inside a reference app ("eleven leaves", "seven such
#: sites", "five modules"). The apps change weekly and nothing re-counts, so a
#: number in a reference is a claim that silently goes false — E012's said
#: "six leaves" while the app had eleven. Describe the shape, not the tally.
_HARD_CODED_COUNT = re.compile(
    r"\b(?:two|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|twenty|\d{1,3})\b(?:\s+\w+){0,2}\s+"
    r"(?:leaves|sites|modules|templates|tests|scenarios|checks|classes|"
    r"subclasses|entries|widgets|files|shims|keys|fields|categories|steps|"
    r"nodes|types|directives|calls|records|findings|entrypoints|suites|jobs)\b",
    re.IGNORECASE,
)


def test_canonical_references_do_not_hard_code_counts() -> None:
    """A reference describes a shape; it must not assert how many of it exist."""
    counted = {
        r.id: m.group(0)
        for r in load_catalog()
        if r.canonical_reference
        and (m := _HARD_CODED_COUNT.search(r.canonical_reference))
    }
    assert not counted, (
        "canonical_reference hard-codes a count that will drift as the app "
        f"changes — describe the shape instead: {counted}"
    )


def test_positive_app_citation_pattern() -> None:
    """A citation needs a path after the app name; a bare mention is not one."""
    assert _POSITIVE_APP_CITATION.search("atlan-mysql-app app/handler.py — …")
    assert _POSITIVE_APP_CITATION.search("atlan-openapi-app pyproject.toml — …")
    assert _POSITIVE_APP_CITATION.search("atlan-metabase-app Dockerfile — …")
    assert not _POSITIVE_APP_CITATION.search(
        "atlan-openapi-app, atlan-mysql-app and atlan-metabase-app each emit …"
    )
    assert not _POSITIVE_APP_CITATION.search("none of the three reference apps")


def test_hard_coded_count_pattern() -> None:
    """The guard fires on real tallies and ignores the reference-app count."""
    assert _HARD_CODED_COUNT.search("app/failures.py — eleven leaves, each …")
    assert _HARD_CODED_COUNT.search("Seven such sites exist across app/extracts/")
    assert _HARD_CODED_COUNT.search("the 15 categorical leaves")
    assert not _HARD_CODED_COUNT.search("none of the three reference apps")
    assert not _HARD_CODED_COUNT.search("every leaf subclasses an SDK category")


def test_canonical_references_never_name_the_scaffold() -> None:
    """``atlan-hello-world-app`` is not a reference app (FND-2477).

    The path check above cannot catch it: a reference naming hello-world *and*
    one of the three apps passes the substring match. Guidance that sends the
    lane to the scaffold is the same defect wherever the lane reads it — every
    prose field of the rule, and the remediation programs (T010's pointer lived
    in ``areas/tests.prose.md`` as well as in the rule).
    """
    scaffold = "atlan-hello-world-app"
    offenders = [
        r.id
        for r in load_catalog()
        if any(
            scaffold in (text or "")
            for text in (
                r.canonical_reference,
                r.full_description,
                r.rationale,
                r.rule_interactions,
                r.terminal_state,
            )
        )
    ]
    programs = Path(conformance.__file__).parent / "programs"
    offenders += [
        str(path.relative_to(programs))
        for path in sorted(programs.rglob("*.prose.md"))
        if scaffold in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"rules / programs that point at {scaffold}, which is not a reference "
        f"app: {offenders}"
    )


#: A ``canonical_reference`` that presents an inline suppression as the
#: compliant shape ("… carries an inline ignore[D003] saying …").
_REFERENCE_IS_A_SUPPRESSION = re.compile(
    r"carr(?:y|ies|ied)\s+an\s+inline\s+(?:`?#?\s*conformance:\s*)?ignore\[",
    re.IGNORECASE,
)


def test_reference_that_is_a_suppression_declares_a_terminal_state() -> None:
    """If the compliant example IS a suppression, the rule must say so in the field.

    A ``canonical_reference`` reading "… carries an inline ignore[X] explaining
    why …" tells a reader that the directive is the end state. But the field
    that a remediation lane actually consults for that is ``terminal_state``,
    and when it is empty the lane sees a rule with findings and no declared
    resting point — so it re-opens settled work every cycle, and a reviewer
    cannot tell a deliberate carve-out from an unfixed violation.

    Worse, prose is not load-bearing: the app that hosted the suppression can
    delete it the moment the checker improves, and the reference then describes
    a file state that no longer exists. D003 and S002 both hit this — D003's
    reference described an ``ignore[D003]`` on ``aiomysql`` that the dialect-
    string checker made unnecessary, and S002's described two directives an app
    removed once the missing seam was reported.

    So: cite a suppression as the compliant shape only alongside a
    ``terminal_state`` that states the condition under which it is correct.
    Better still, point the reference at code that needs no suppression.
    """
    offenders = [
        r.id
        for r in load_catalog()
        if r.canonical_reference
        and _REFERENCE_IS_A_SUPPRESSION.search(r.canonical_reference)
        and not r.terminal_state
    ]
    assert not offenders, (
        "canonical_reference presents an inline suppression as the compliant "
        f"shape but no terminal_state says when that is correct: {offenders} — "
        "either declare terminal_state, or point the reference at code that "
        "needs no suppression"
    )


def test_reference_suppression_pattern_does_not_over_match() -> None:
    """The guard must fire on the real shape and not on a passing mention."""
    assert _REFERENCE_IS_A_SUPPRESSION.search(
        "atlan-mysql-app pyproject.toml — aiomysql ... carries an inline "
        "ignore[D003] saying SQLAlchemy loads it dynamically"
    )
    assert _REFERENCE_IS_A_SUPPRESSION.search(
        "the two os.environ writes carry an inline ignore[S002] explaining that"
    )
    # A reference that merely says no suppression is needed must not trip it.
    assert not _REFERENCE_IS_A_SUPPRESSION.search(
        "app/handler.py preflight_check returns a typed row, which the rule "
        "detects — no suppression needed"
    )
    assert not _REFERENCE_IS_A_SUPPRESSION.search(
        "aiomysql is declared with no Python import and carries no suppression"
    )


def test_canonical_references_are_not_shared() -> None:
    """No two rules may point at the same place.

    A reference reused verbatim across a family is a description of the family,
    not of the rule — nine contract rules once shared one line naming the whole
    generated tree, which cannot tell a reader which of the nine they tripped.
    Uniqueness is the cheapest available proof that each was read from the file
    it names.
    """
    seen: dict[str, list[str]] = {}
    for rule in load_catalog():
        if rule.canonical_reference:
            seen.setdefault(rule.canonical_reference, []).append(rule.id)
    shared = {ref: ids for ref, ids in seen.items() if len(ids) > 1}
    assert not shared, (
        "canonical_reference is shared by several rules, so it is too coarse to "
        f"have been read from either: { {ref[:60]: ids for ref, ids in shared.items()} }"
    )

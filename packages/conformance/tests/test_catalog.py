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
from conformance.suite.schema.extensions import AtlanRuleProperties
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
    # (BLDX-1499).
    # P025: app-name alignment — only apps have an atlan.yaml and .env.example;
    # the SDK has neither, so this check is meaningless there (BLDX-1491).
    # P029/P030 + P037/P038/P039/P042: SDR-readiness — only apps declare
    # self_deployed_runtime; the SDK itself never does, so these are APP-scoped.
    # P032–P035: preflight-gate authoring — only apps register @task activities,
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
    # P043/P045: error-seam — apps must build control flow on the SDK's public
    # error surface (application_sdk.errors.__all__), not on an internal error
    # class that can move, or stop being the one a boundary raises, in a minor
    # release. The SDK is the publisher of that surface, so neither rule grades
    # it (CONNECT-970).
    assert app_scoped == {
        "B001",
        "B007",
        "D010",
        "P040",
        "P042",
        "P043",
        "P044",
        "P045",
        "P047",
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
        "P032",
        "P033",
        "P034",
        "P035",
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
    }, app_scoped
    # SDK-only rules: the SDK must keep Temporal contained behind its seam
    # (P006/P007, BLDX-1417), declare its deprecations correctly (B002–B004),
    # and keep its text file IO locale-independent (P046) — the SDK repo is the
    # only one in the fleet that runs a Windows CI leg, where the platform
    # default codec is cp1252 rather than UTF-8.
    sdk_scoped = {r.id for r in rules if r.scope == RuleScope.SDK}
    assert sdk_scoped == {
        "B002",
        "B003",
        "B004",
        "P006",
        "P007",
        "P046",
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
    P032–P035 are the preflight-gate rules — reserved gate-name collision,
    duplicate in-workflow preflight, untyped check failures, and metadata /
    input-contract parity (BLDX-1545).
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
        "P032",
        "P033",
        "P034",
        "P035",
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
        "P047",
    }
    missing = expected - p_ids
    assert not missing, f"Missing P-series rules: {missing}"
    extra = p_ids - expected
    assert not extra, f"Unexpected P-series rules: {extra}"


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
    expected = {"B001", "B002", "B003", "B004", "B005", "B006", "B007"}
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
    assert descriptor.properties["atlan/autofixable"] is False
    assert descriptor.properties["atlan/orthogonalGate"] == "tests"


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
        }
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

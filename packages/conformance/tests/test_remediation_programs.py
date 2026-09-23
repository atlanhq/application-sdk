"""Wiring tests for the shipped OpenProse remediation programs.

The programs are executed by an agent, not by an interpreter, so nothing else in
CI notices when a parameter is added to a contract's signature but not threaded
through its callers. The failure is silent and expensive: a run scoped to one rule
that quietly widens to a whole series, or a suggest-only area that starts applying
because a default went missing.

These tests read the shipped contracts as text and assert the threading holds.
They deliberately assert on the *call sites*, not on prose wording, so
rewording a contract does not break them but dropping an argument does.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

PROGRAMS = files("conformance").joinpath("programs")

# Areas whose default is propose-don't-apply, and which therefore take
# `apply_unverifiable`. Kept explicit rather than derived: the point of the test
# is to notice if a fifth area quietly joins them.
SUGGEST_ONLY_AREAS = (
    "prescriptions-area",
    "preflight-area",
    "dockerfile-area",
    "security-area",
)


def _read(rel: str) -> str:
    return PROGRAMS.joinpath(rel).read_text()


@pytest.fixture(scope="module")
def top_level() -> str:
    return _read("conformance-remediation.prose.md")


@pytest.fixture(scope="module")
def loop() -> str:
    return _read("patterns/detect-fix-recheck.prose.md")


# ── rule_ids threading ────────────────────────────────────────────────────


def test_detect_violations_documents_rule_ids_as_a_post_filter() -> None:
    """The contract states rule scoping is a post-filter, and why.

    A future reader who assumes `--series L004` works will produce an empty report
    and conclude the rule is clean. The contract has to say so.
    """
    text = _read("functions/detect-violations.prose.md")
    assert "`rule_ids`" in text
    assert "post-filter" in text
    assert "result.rule_id" in text
    # The specific trap: --series matches a series LETTER, so --series L004
    # activates zero checks rather than filtering to L004.
    assert "--series L004" in text or "--series <rule>" in text


def _call_blocks(text: str, callee: str) -> list[str]:
    """Every `call <callee>` block in a prose file, as its parameter lines.

    Structural, not a single regex over the whole file: a block ends where the
    parameter indentation does, so an assertion against one block can't be
    satisfied by a parameter that actually belongs to a different call — which
    is exactly how the original tests passed while the area→loop hop dropped
    `rule_ids`.
    """
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == f"call {callee}" or stripped.endswith(f"= call {callee}"):
            indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1
            body: list[str] = []
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent or nxt.lstrip().startswith("#"):
                    if nxt.lstrip().startswith("#"):
                        j += 1
                        continue
                    break
                body.append(nxt)
                j += 1
            blocks.append("\n".join(body))
            i = j
        else:
            i += 1
    return blocks


ALL_AREAS = [
    "ci",
    "contract-toolkit",
    "dependency",
    "deprecation",
    "dockerfile",
    "error-handling",
    "logging",
    "optimizations",
    "preflight",
    "prescriptions",
    "security",
    "tests",
]


def test_every_catalog_series_has_a_remediation_area() -> None:
    """A series with rules but no area drops out of /remediate silently.

    The dispatcher only calls the areas it names and nothing else notices, so
    the findings are never remediated — indistinguishable from a clean repo.
    """
    from conformance.suite.rules import CATALOG

    series = {rule_id[0] for rule_id in CATALOG}
    covered: set[str] = set()
    for area in ALL_AREAS:
        covered.update(
            re.findall(r'series: "([A-Z])"', _read(f"areas/{area}.prose.md"))
        )
    orphaned = sorted(series - covered)
    assert not orphaned, f"catalog series with no remediation area: {orphaned}"

    tagging = _read("functions/detect-violations.prose.md")
    tagged = dict(re.findall(r"`([A-Z])` → `([a-z-]+)`", tagging))
    untagged = sorted(series - set(tagged))
    assert not untagged, f"detect-violations tags no area for series: {untagged}"
    assert set(tagged.values()) <= set(ALL_AREAS), tagged

    dispatch = _read("functions/remediate-finding.prose.md")
    undispatched = [area for area in ALL_AREAS if f"| `{area}` |" not in dispatch]
    assert not undispatched, f"remediate-finding dispatches no row for: {undispatched}"


@pytest.mark.parametrize("area", ALL_AREAS)
def test_every_area_forwards_rule_ids_into_every_runner_call(area: str) -> None:
    """THE hop that was actually broken (sdk-review on this PR, F1): the
    dispatcher threaded `rule_ids` to every area and every area then dropped it,
    so `--rule L004` silently widened to the whole series. Assert the area→loop
    hop and the suggest-only detect calls — every place an area invokes the
    runner."""
    text = _read(f"areas/{area}.prose.md")
    blocks = _call_blocks(text, "detect-fix-recheck") + _call_blocks(
        text, "detect-violations"
    )
    assert blocks, f"{area} makes no runner calls — the test would be vacuous"
    missing = [b for b in blocks if "rule_ids: rule_ids" not in b]
    assert not missing, (
        f"{area}: {len(missing)} runner call(s) do not forward rule_ids — "
        f"a --rule-scoped run widens to the whole series at this hop:\n"
        + "\n---\n".join(missing)
    )


@pytest.mark.parametrize("area", ALL_AREAS)
def test_every_area_declares_rule_ids(area: str) -> None:
    assert "`rule_ids`" in _read(
        f"areas/{area}.prose.md"
    ), f"{area} forwards rule_ids but never declares it as a parameter"


def test_o001_prescription_warns_orjson_bypasses_default() -> None:
    """orjson serializes datetime/date/time/UUID/dataclasses natively and never
    consults ``default``. NumPy is not native unless ``OPT_SERIALIZE_NUMPY`` is
    set. ``json.dumps`` supports none of them, so a ``default=`` on a stdlib
    call is very often there to encode exactly one of those — and the swap
    silently stops calling it.

    The prescription used to say only that ``default=`` "stays as the default
    keyword (orjson supports it)", which is true and, on its own, misleading: the
    finding clears, the output shape changes, and a re-detect reports a clean
    fix. It cost a connector every date attribute on its published assets
    (ATLAS-404-00-007), caught by a pre-existing unit test rather than by any
    gate.

    So the prose must name the bypass AND the passthrough options that restore
    the old behaviour (datetime *and* dataclass), plus the NumPy qualifier so
    the native-type inventory cannot regress.
    """
    text = _read("areas/optimizations.prose.md")
    start = text.index("**O001 OrjsonOverStdlibJson**")
    prescription = text[start : text.index("**O002", start)]

    for needle in (
        "default",
        "OPT_PASSTHROUGH_DATETIME",
        "OPT_PASSTHROUGH_DATACLASS",
        "OPT_SERIALIZE_NUMPY",
        "datetime",
    ):
        assert needle in prescription, (
            f"O001's prescription does not mention {needle!r} — a `default=` that "
            "encodes a natively-serialized type will be silently bypassed by the "
            "swap this rule prescribes"
        )


REFERENCE_APPS = (
    "atlan-mysql-app",
    "atlan-metabase-app",
    "atlan-openapi-app",
)


def test_hello_world_is_not_a_remediation_reference() -> None:
    """Owner decision (FND-2477): the scaffold app is too minimal to be what a
    fix is mirrored from. Neither the prose nor the vendored skill may send a
    model there."""
    for rel in (
        "functions/remediate-finding.prose.md",
        "functions/detect-violations.prose.md",
    ):
        assert "atlan-hello-world-app" not in _read(rel), rel
    template = (
        files("conformance").joinpath("bootstrap/templates/remediate.md").read_text()
    )
    assert "atlan-hello-world-app" not in template


def test_remediate_finding_requires_the_reference_apps() -> None:
    """A small model must not fix from memory: the contract has to name the
    three reference apps, tell the model to load the full checkout, and thread
    the per-rule pointer (`canonical_reference`) into the finding it reads."""
    text = _read("functions/remediate-finding.prose.md")
    for app in REFERENCE_APPS:
        assert app in text, f"remediate-finding never names {app}"
    assert "`canonical_reference`" in text
    # Outside the repo: an in-repo clone is scanned by detect (FND-2682).
    assert "atlan-conformance/refs" in text
    assert "remediation/refs" not in text
    assert "git clone" in text


def test_remediate_finding_declares_impact_and_verification() -> None:
    """The result must carry what was checked before the edit and what was
    verified after it — a reviewer reads evidence, not an outcome — and a
    migration rule must leave a brief instead of an edit."""
    text = _read("functions/remediate-finding.prose.md")
    for field in ("`impact`", "`verification`", "`migration_brief`"):
        assert field in text, f"remediate-finding does not declare {field}"
    for check in (
        "finding_cleared",
        "gate_passed",
        "no_new_findings",
        "matches_reference",
    ):
        assert check in text, f"verification does not name {check}"
    assert "autofixable == false" in text
    assert "not_remediable = true" in text


def test_remediate_finding_reviews_consequences_after_verification() -> None:
    """Verification proves the finding is gone; the consequence review proves
    the app still works. Both halves of `impact` must be named."""
    text = _read("functions/remediate-finding.prose.md")
    assert "`impact.after`" in text
    assert "Review the consequences after verification" in text
    for surface in ("control flow", "signatures and types", "runtime surfaces"):
        assert surface in text, f"consequence review does not cover {surface}"


def test_suppression_is_a_rule_defect_signal() -> None:
    """A suppression that is really a false positive or a prescription defect
    must become a PR against the suite, not a silent ignore directive."""
    text = _read("functions/remediate-finding.prose.md")
    for field in ("`suppression_reason`", "`rule_defect_pr`"):
        assert field in text, f"remediate-finding does not declare {field}"
    for reason in ("site-exception", "false-positive", "prescription-defect"):
        assert reason in text, f"suppression_reason value {reason} not named"
    assert "report-rule-defect" in text
    # Line-wrapped prose: assert on the phrase that starts the sentence.
    assert "Never suppress a BLOCK-tier" in text


@pytest.mark.parametrize("area", ["error-handling", "logging"])
def test_exc_info_prescriptions_carry_the_credential_contraindication(
    area: str,
) -> None:
    """Adding `exc_info=True` at a connect/auth site creates a credential leak.

    The traceback is serialised separately, so it bypasses whatever redaction
    the message performs — FND-57 found this shape in five connector repos.
    Both areas prescribe adding `exc_info=True` (E004/E005/E007/E009/E014,
    L004/L005/L017), so both must carry the contraindication and must point at
    the sanitizer form, which clears the rule with no suppression.
    """
    text = _read(f"areas/{area}.prose.md")
    assert "Credential-boundary contraindication" in text, (
        f"{area} prescribes adding exc_info=True with no credential-leak "
        "contraindication"
    )
    # The safe fix has to name a helper the checker actually recognises —
    # recognition is by name (_ast_common/_sanitizers.py), so a correct but
    # unrecognised helper would leave the finding standing.
    assert "sanitize_cause_repr" in text
    assert "application_sdk.errors" in text
    # And it must say the sanitized form is a fix, not a carve-out.
    assert "no suppression" in text


def test_b006_may_write_the_contract_ledger() -> None:
    """B006's only remedy writes `contract_schema.lock.json` at the repo root,
    which is neither Python source nor the Dockerfile.

    Without an explicit carve-out in the write-scope section the loop applies
    nothing, and — worse since step 5 exists — the model reads its own refusal
    as a `prescription-defect` and opens a spurious PR against this repo. The
    flag and the carve-out have to move together, so assert both: B006 is
    auto-fixable, and the write scope names the file for it.
    """
    from conformance.suite.rules import get_rule

    assert get_rule("B006").autofixable is True, (
        "B006 is no longer auto-fixable — if that is deliberate, remove the "
        "write-scope carve-out for contract_schema.lock.json with it."
    )
    text = _read("functions/remediate-finding.prose.md")
    scope = text.split("### Write-scope constraint")[1].split("### Reference apps")[0]
    assert "contract_schema.lock.json" in scope, (
        "the write-scope section no longer permits B006 to write "
        "contract_schema.lock.json — the rule becomes unfixable by construction"
    )
    assert "B006" in scope, "the ledger carve-out no longer names B006"


def test_report_rule_defect_contract_is_bounded() -> None:
    """The cross-repo PR is the one place the remediator may touch the gate,
    so the contract has to state the bounds: dedup, reproducer that fails on
    main, never merge, never edit the app's own gate, and a draft fallback when
    the token cannot reach application-sdk."""
    text = _read("functions/report-rule-defect.prose.md")
    assert "gh pr list" in text
    assert "fail on `main`" in text
    assert "xfail(strict=True" in text
    assert "fix(conformance):" in text
    assert "Never merge, approve or enable auto-merge" in text
    assert "`draft`" in text
    assert "secret values redacted" in text
    assert "atlanhq/application-sdk" in text


def test_loop_rejects_rule_defect_suppression_without_a_pr(loop: str) -> None:
    """The check runs before the directive is written, like the evidence check,
    and BLOCK-tier defects are never suppressed."""
    body = loop.split("### Delegation")[1]
    guard_at = body.index("result.suppression_reason")
    apply_at = body.index("apply result.edit")
    assert guard_at < apply_at, "rule-defect guard must precede apply"
    assert "not result.rule_defect_pr" in body
    assert 'finding.disposition == "failing"' in body
    emit = body.split("emit residue as structured report")[1]
    assert "rule_defect_pr" in emit


def test_detect_violations_surfaces_the_canonical_reference() -> None:
    """The pointer is only useful if the finding carries it."""
    text = _read("functions/detect-violations.prose.md")
    assert "atlan/canonicalReference" in text
    assert "`canonical_reference`" in text


def test_loop_carries_the_brief_and_reports_verification(loop: str) -> None:
    """The loop must not flatten a migration brief into a generic note, and the
    residue report must show impact/verification next to every item."""
    body = loop.split("### Delegation")[1]
    assert "result.migration_brief" in body
    emit = body.split("emit residue as structured report")[1]
    assert "result.impact" in emit
    assert "result.verification" in emit
    assert "migration_brief" in emit


def test_bootstrapped_skill_tells_the_runner_to_load_the_reference_apps() -> None:
    """The vendored SKILL.md is what a headless lane actually reads; the
    reference-app duty has to be stated there, not only in the prose."""
    template = (
        files("conformance").joinpath("bootstrap/templates/remediate.md").read_text()
    )
    for app in REFERENCE_APPS:
        assert app in template, f"bootstrap remediate.md never names {app}"
    assert "atlan-conformance/refs" in template
    assert "remediation/refs" not in template
    assert "migration_brief" in template
    assert "report-rule-defect" in template
    assert "impact.after" in template


def test_remediate_finding_declares_the_evidence_field() -> None:
    """`require_cited_evidence` gates on `result.evidence`; the producer contract
    must declare it or the blind-gate areas key off an unspecified model field
    (sdk-review on this PR, F2)."""
    text = _read("functions/remediate-finding.prose.md")
    assert "`evidence`" in text
    assert "require_cited_evidence" in text


@pytest.mark.parametrize("area", ["prescriptions", "preflight", "security"])
def test_blind_gate_prescriptions_point_at_result_evidence(area: str) -> None:
    assert "result.evidence" in _read(f"areas/{area}.prose.md")


def test_docker_build_gate_fails_closed_when_no_dockerfile_is_found() -> None:
    """A gate that passes when its subject goes missing is an always-pass path
    (sdk-review on this PR, F4): a fix could omit the Dockerfile from
    model-reported touched_files and sail through."""
    text = _read("functions/docker-build-gate.prose.md")
    assert "fail closed" in text
    assert "finding.file" in text
    # The old fast-pass must be gone.
    assert "nothing to build" not in text


def test_deliver_as_draft_is_stamped_in_the_loop_body_not_just_documented(
    loop: str,
) -> None:
    """The flag must be stamped onto the loop's outputs, or the S-series draft
    guarantee silently becomes unenforceable downstream (sdk-review, F3 — the
    first fix documented the stamp without performing it, so this test reads
    the DELEGATION BODY, not the parameter prose)."""
    body = loop.split("### Delegation")[1]
    # Surviving results are stamped where classification_override lands...
    assert "let result.deliver_as_draft = true" in body
    # ...residue items are stamped before the report is emitted...
    assert "let item.deliver_as_draft = true" in body
    # ...and the emitted report carries the promised column.
    emit = body.split("emit residue as structured report")[1]
    assert "deliver_as_draft" in emit


def test_deliver_as_draft_names_its_consumer(loop: str) -> None:
    """Stamping is only half the contract — the prose must say who reads it
    (the playbook's delivery stage and the /remediate residue phase)."""
    assert "residue report" in loop.split("`deliver_as_draft`")[1][:900]


def test_every_detect_call_in_the_loop_threads_rule_ids(loop: str) -> None:
    """Both detect-violations calls in the loop forward rule_ids.

    The end-of-round re-detect matters as much as the first call: if it widens to
    the whole series, the loop compares a one-rule fingerprint set against a
    whole-series one, never converges, and escalates a rule it actually fixed.
    """
    calls = re.findall(r"call detect-violations\n((?:\s{2,}\w+:.*\n)+)", loop)
    assert len(calls) == 2, f"expected 2 detect-violations calls, found {len(calls)}"
    for i, body in enumerate(calls):
        assert "rule_ids: rule_ids" in body, (
            f"detect-violations call #{i + 1} in detect-fix-recheck.prose.md does "
            "not forward rule_ids"
        )


def test_top_level_threads_rule_ids_to_every_area(top_level: str) -> None:
    """Every area call forwards rule_ids — a missed one silently ignores --rule."""
    area_calls = re.findall(r"call ([a-z\-]+-area)\n((?:\s{4}\w+:.*\n)+)", top_level)
    assert len(area_calls) >= 11, f"expected >=11 area calls, found {len(area_calls)}"
    missing = [name for name, body in area_calls if "rule_ids: rule_ids" not in body]
    assert not missing, f"area call(s) not forwarding rule_ids: {missing}"


# ── apply_unverifiable threading ──────────────────────────────────────────


def test_apply_unverifiable_goes_to_exactly_the_suggest_only_areas(
    top_level: str,
) -> None:
    """Only P/F/I/S take the flag.

    Threading it into an area that already applies would be meaningless; missing it
    on one of these four makes `--apply-unverifiable` a partial no-op that looks
    like it worked.
    """
    area_calls = re.findall(r"call ([a-z\-]+-area)\n((?:\s{4}\w+:.*\n)+)", top_level)
    got = {
        name
        for name, body in area_calls
        if "apply_unverifiable: apply_unverifiable" in body
    }
    assert got == set(SUGGEST_ONLY_AREAS), (
        f"apply_unverifiable threaded to {sorted(got)}, expected "
        f"{sorted(SUGGEST_ONLY_AREAS)}"
    )


@pytest.mark.parametrize(
    "area", ["prescriptions", "preflight", "dockerfile", "security"]
)
def test_suggest_only_area_declares_the_flag_and_keeps_a_default_path(
    area: str,
) -> None:
    """Each area documents the requirement, defaults false, and keeps both branches.

    Defaulting false is the compatibility guarantee: a caller that does not opt in
    must get byte-identical behaviour to before the flag existed.
    """
    text = _read(f"areas/{area}.prose.md")
    assert "`apply_unverifiable`" in text, f"{area} does not declare apply_unverifiable"
    assert "default `false`" in text, f"{area} does not state the false default"
    assert "if apply_unverifiable:" in text, f"{area} has no opt-in branch"
    assert "\nelse:" in text, f"{area} lost its propose-only branch"


@pytest.mark.parametrize("area", ["prescriptions", "preflight", "security"])
def test_blind_gate_areas_force_the_unverifiable_classification(area: str) -> None:
    """P, F and S must not be able to emit a result that reads as gate-verified.

    Their gates pass on any edit, so the classification is the only thing standing
    between "applied" and "verified". The dockerfile area is deliberately excluded:
    its docker-build gate is real, so its fixes are genuinely verified.
    """
    text = _read(f"areas/{area}.prose.md")
    assert 'classification_override: "unverifiable"' in text
    assert "require_cited_evidence: true" in text


def test_dockerfile_area_does_not_claim_to_be_unverifiable() -> None:
    """I-series is gated for real, so it must not force the unverifiable label."""
    text = _read("areas/dockerfile.prose.md")
    assert "classification_override" not in text
    assert "docker-build" in text


def test_security_area_delivers_as_draft() -> None:
    """A credential relocation cannot merge on a green check alone."""
    assert "deliver_as_draft: true" in _read("areas/security.prose.md")


def test_security_area_forbids_moving_the_value() -> None:
    """The area states the value/reference boundary explicitly."""
    text = _read("areas/security.prose.md")
    assert "Never move a secret value" in text


# ── loop honours the new parameters ───────────────────────────────────────


def test_loop_declares_all_new_parameters(loop: str) -> None:
    for param in (
        "`rule_ids`",
        "`classification_override`",
        "`require_cited_evidence`",
        "`deliver_as_draft`",
    ):
        assert param in loop, f"detect-fix-recheck.prose.md does not declare {param}"


def test_uncited_fix_is_rejected_before_it_is_applied(loop: str) -> None:
    """The evidence check precedes `apply result.edit`.

    Checking after applying would leave a guessed value on disk between the write
    and the revert, and would make the revert path load-bearing for correctness
    rather than for cleanup.
    """
    evidence_at = loop.index("require_cited_evidence and")
    apply_at = loop.index("apply result.edit")
    assert (
        evidence_at < apply_at
    ), "the cited-evidence check must run before the edit is applied"


def test_unverifiable_always_routes_to_residue(loop: str) -> None:
    """`unverifiable` is in the residue condition alongside `judgment`."""
    assert 'result.classification == "unverifiable"' in loop


# ── docker-build gate ─────────────────────────────────────────────────────


def test_docker_build_gate_never_passes_when_docker_is_absent() -> None:
    """A gate that cannot run must not report success.

    This is the whole reason the I-series can be allowed to apply: if the gate
    silently passed on a machine without a daemon, `--apply-unverifiable` would be
    accepting Dockerfile edits on trust, which is exactly the state the area was
    written to avoid.
    """
    text = _read("functions/docker-build-gate.prose.md")
    assert "docker_absent" in text
    assert "`passed` is always false" in text
    # `which docker` is not sufficient: a client with no daemon would pass it.
    assert "docker info" in text


def test_docker_build_gate_cleans_up_its_image() -> None:
    """A long sweep must not accumulate one image per remediated rule."""
    text = _read("functions/docker-build-gate.prose.md")
    assert "docker image rm" in text


# ── the area's tier list must match the catalog ────────────────────────────


def test_dependency_area_lists_every_warn_tier_d_rule() -> None:
    """A WARN-tier rule missing from this list is skipped by strict-mode
    `/remediate`, silently.

    The list drives which findings the area processes in strict mode. Adding a
    WARN rule to the catalog and forgetting the prose costs nothing at import
    time, produces no warning, and the only symptom is that the rule is never
    remediated — indistinguishable from a repo that has no such finding. D014
    shipped exactly that way and was caught in review, not by a test.
    """
    from conformance.suite.rules import CATALOG

    text = _read("areas/dependency.prose.md")
    listed = set(re.findall(r"\bD\d{3}\b", text.split("### The re-detection")[0]))
    expected = {
        rule_id
        for rule_id, rule in CATALOG.items()
        if rule_id.startswith("D") and rule.tier.value.upper() == "WARN"
    }
    missing = sorted(expected - listed)
    assert not missing, (
        f"WARN-tier D-rule(s) {missing} are not named in the dependency area's "
        "violation-set, so strict-mode /remediate will skip their findings. Add "
        "them to the WARN-tier list and give each a Fix Prescription entry."
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


def _autofixable_rules_without_a_bullet() -> set[str]:
    from conformance.suite.rules import CATALOG
    from conformance.suite.schema.disposition import RuleScope

    missing: set[str] = set()
    for rule in CATALOG.values():
        if rule.scope is RuleScope.SDK or not rule.autofixable:
            continue
        text = _read(f"areas/{SERIES_AREA[rule.id[0]]}.prose.md")
        if not re.search(r"\*\*" + rule.id + r"\b", text):
            missing.add(rule.id)
    return missing


def test_every_autofixable_rule_has_a_per_rule_prescription() -> None:
    """An auto-fixable rule the lane may act on must tell the model what the
    edit is — a `**<ID> Name**` bullet in its area's Fix Prescription, not a
    catch-all "fix guided by the hint".

    A catch-all tells a cheap model to use judgement, which is the one thing
    the classification exists to remove: marking a rule auto-fixable is a claim
    that the edit is known. Two failure shapes this closes, both live before
    FND-2477: B006 was flagged auto-fixable with no prescription anywhere, so
    `/remediate` returned not_remediable on 415 BLOCK findings; and 20 more
    rules were covered only by their area's catch-all paragraph.

    There is no exemption list on purpose. A new auto-fixable rule without a
    bullet fails here, and the honest ways out are to write the prescription or
    to classify the rule as migration.
    """
    missing = sorted(_autofixable_rules_without_a_bullet())
    assert not missing, (
        "auto-fixable rule(s) with no per-rule `**<ID> Name**` prescription "
        f"bullet in their area's Fix Prescription: {missing}. Write the bullet "
        "(derive the edit from the checker predicate, not the short "
        "description), or classify the rule as migration."
    )


def test_dependency_area_has_a_prescription_for_every_d_rule() -> None:
    """Being listed as WARN-tier is only half of it: the area also has to say
    what to do with the finding, or the loop reaches it with no instruction."""
    from conformance.suite.rules import CATALOG

    text = _read("areas/dependency.prose.md")
    prescriptions = set(re.findall(r"\*\*(D\d{3}) [A-Za-z]", text))
    expected = {rule_id for rule_id in CATALOG if rule_id.startswith("D")}
    missing = sorted(expected - prescriptions)
    assert not missing, (
        f"D-rule(s) {missing} have no Fix Prescription entry in the dependency "
        "area. Every rule the loop can reach needs one, even if it is "
        "`not_remediable = true` and routes straight to residue."
    )


def _rule_bullet(area: str, rule_id: str) -> str:
    """The `**<ID> Name**` bullet for one rule, up to the next top-level bullet."""
    text = _read(f"areas/{area}.prose.md")
    match = re.search(r"^- \*\*" + rule_id + r"\b.*?(?=^- \*\*|\Z)", text, re.M | re.S)
    assert match, f"no `**{rule_id}` bullet in areas/{area}.prose.md"
    return match.group(0)


def test_b005_sunset_marker_is_the_one_the_ledger_generator_reads() -> None:
    """The B005 retirement path names the exact source marker, and it is one
    `gen-contract-ledger` actually reads back as `sunset`.

    Before this, the rule offered three different mechanisms: hand-edit the
    ledger, mark it "in the Pkl widget definition" (a Python Output contract has
    none), or the prose's bare "deprecate and sunset it". The only one the
    generator recognises was documented under P001, not under the rule that
    needs it. An app remediation run had to read `_field_status` to find it,
    and the rule's own finding had been suppressed for want of it.
    """
    import ast

    from conformance.suite.checks._entrypoint_contract_fields import _field_status
    from conformance.suite.rules import CATALOG

    bullet = _rule_bullet("deprecation", "B005")
    snippets = re.findall(r"`(Field\([^`]*x-lifecycle[^`]*\))`", bullet)
    assert snippets, "B005 prescription names no `Field(... x-lifecycle ...)` marker"
    for snippet in snippets:
        source = "x: int = " + snippet.replace("<zero value>", "0")
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.AnnAssign)
        assert _field_status(node) == "sunset", snippet

    rule = CATALOG["B005"]
    for text in (rule.terminal_state, rule.full_description):
        assert "x-lifecycle" in text
        assert "Pkl widget" not in text


def test_o001_prescription_names_the_byte_changing_defaults() -> None:
    """A stdlib `json.dumps` with default arguments does not round-trip through
    orjson byte-for-byte: orjson is always compact and never escapes non-ASCII.

    The parsed value is unchanged, so the orthogonal gate passes, and the only
    place the difference shows is whatever hashes, commits or byte-compares the
    output. Found on an app whose vendor-contract refresh script rewrites a
    committed, `\\u`-escaped JSON file: the prescribed `indent=2 → OPT_INDENT_2`
    swap would have un-escaped 30 lines of it the next time it ran.
    """
    bullet = _rule_bullet("optimizations", "O001")
    assert "ensure_ascii" in bullet
    assert "separators" in bullet

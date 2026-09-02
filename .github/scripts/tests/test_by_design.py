"""The suppression list, as a mechanism instead of a request.

Every test here guards a failure that is silent in production. A suppression
that does not fire costs the author a round explaining an intentional pattern
for the Nth time; a suppression that fires too widely deletes a real defect on
its way to the summary and nobody ever learns it existed. The second is far
worse, which is why most of this file is about over-suppression rather than
under-suppression.

The shipped `by_design.yaml` is exercised directly rather than through
fixtures wherever the point is that the real data behaves — a test that only
proves a synthetic entry works would pass happily while the file we actually
load is malformed.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_by_design as bd  # noqa: E402
from sdk_loop_findings import (  # noqa: E402
    Finding,
    audit_comment,
    compute_verdict,
    load_severity,
    normalise,
)

DATA = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".mothership/pr-loop/data/by_design.yaml"
)


def _f(**kw) -> Finding:
    """A finding with the fields the filter reads, defaulted to non-matching."""
    base = dict(
        title="t",
        severity="HIGH",
        confidence=0.9,
        file="application_sdk/app/thing.py",
        evidence="x = 1",
    )
    base.update(kw)
    return Finding(**base)


# ---------------------------------------------------------------------------
# The shipped data loads and is internally consistent
# ---------------------------------------------------------------------------


def test_the_shipped_data_loads() -> None:
    """A malformed data file would fail at review time, mid-round, on a runner."""
    loaded = bd.load_by_design(DATA)
    assert loaded.entries, "by_design.yaml suppresses nothing"
    assert loaded.never_ci_owned, "never_ci_owned is empty — the ci-gate guard is inert"


def test_no_ci_gate_entry_claims_a_rule_ci_does_not_block() -> None:
    """The one claim in the file that can be factually false.

    A `ci-gate` entry asserts a deterministic gate already blocks the pattern.
    When that is wrong for a WARN-tier conformance rule, the rule ends up
    enforced by nobody: CI surfaces it without blocking, and the reviewer has
    been told to stay quiet. The real file must not contain one.
    """
    loaded = bd.load_by_design(DATA)
    for entry in loaded.entries:
        if entry.owner == "ci-gate":
            assert not (
                entry.pattern_ids & loaded.never_ci_owned
            ), f"{entry.id} claims CI owns {sorted(entry.pattern_ids & loaded.never_ci_owned)}"


def test_a_ci_gate_entry_over_a_guardrail_pattern_fails_to_load(tmp_path) -> None:
    """G2/G3 findings carry `io-in-workflow` / `field-removed`, never the aliases.

    Listing `G2-contract-evolution` in never_ci_owned made the load-time guard
    inert for the guardrails it claimed to protect: a later `ci-gate` entry
    naming the real id would load, and the guardrail would then be enforced
    by nobody. Same shape as the WARN-tier test below; different ids.
    """
    bad = tmp_path / "by_design.yaml"
    bad.write_text(
        "version: 1\n"
        "never_ci_owned: [io-in-workflow]\n"
        "suppress:\n"
        "  - id: wrong\n"
        "    owner: ci-gate\n"
        "    reason: CI blocks it\n"
        "    match:\n"
        "      pattern_ids: [io-in-workflow]\n",
        encoding="utf-8",
    )
    with pytest.raises(bd.ByDesignError, match="never_ci_owned"):
        bd.load_by_design(bad)


def test_never_ci_owned_lists_the_real_g2_g3_pattern_ids() -> None:
    """The shipped file must name what findings actually emit."""
    loaded = bd.load_by_design(DATA)
    for pid in (
        "field-removed",
        "field-renamed",
        "field-type-changed",
        "io-in-workflow",
        "non-deterministic-in-workflow",
    ):
        assert pid in loaded.never_ci_owned, pid
    assert "G2-contract-evolution" not in loaded.never_ci_owned
    assert "G3-determinism" not in loaded.never_ci_owned


def test_a_ci_gate_entry_over_a_warn_tier_rule_fails_to_load(tmp_path) -> None:
    """Asserting the guard fires, not merely that today's file is clean."""
    bad = tmp_path / "by_design.yaml"
    bad.write_text(
        "version: 1\n"
        "never_ci_owned: [MissingExceptionChaining]\n"
        "suppress:\n"
        "  - id: wrong\n"
        "    owner: ci-gate\n"
        "    reason: CI blocks it\n"
        "    match:\n"
        "      pattern_ids: [MissingExceptionChaining]\n",
        encoding="utf-8",
    )
    with pytest.raises(bd.ByDesignError, match="never_ci_owned"):
        bd.load_by_design(bad)


def test_the_same_rule_may_be_suppressed_by_design_with_a_path_scope(tmp_path) -> None:
    """The counterpart to the test above, and the reason the guard is narrow.

    `DirectTemporalImport` is WARN-tier, so no `ci-gate` entry may claim it —
    but inside `execution/_temporal/` it is the adapter seam and genuinely not
    a defect. A guard that blocked the rule id outright would make that
    unexpressible and force the suppression to be mislabelled as ci-gate.
    """
    ok = tmp_path / "by_design.yaml"
    ok.write_text(
        "version: 1\n"
        "never_ci_owned: [DirectTemporalImport]\n"
        "suppress:\n"
        "  - id: seam\n"
        "    owner: by-design\n"
        "    reason: this path is the adapter\n"
        "    match:\n"
        "      paths: ['**/execution/_temporal/**']\n"
        "      pattern_ids: [DirectTemporalImport]\n",
        encoding="utf-8",
    )
    loaded = bd.load_by_design(ok)
    inside = _f(
        pattern_id="DirectTemporalImport",
        file="application_sdk/execution/_temporal/w.py",
    )
    outside = _f(pattern_id="DirectTemporalImport", file="application_sdk/app/base.py")
    assert loaded.match(inside) is not None
    assert loaded.match(outside) is None, "the seam suppression escaped its path"


def test_an_empty_match_is_refused(tmp_path) -> None:
    """An entry with no criteria matches every finding — a silent kill switch."""
    bad = tmp_path / "by_design.yaml"
    bad.write_text(
        "version: 1\nsuppress:\n  - id: oops\n    owner: by-design\n"
        "    reason: r\n    match: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(bd.ByDesignError, match="suppress every finding"):
        bd.load_by_design(bad)


def test_every_entry_carries_a_reason(tmp_path) -> None:
    """A suppression nobody can justify later is one nobody can review."""
    bad = tmp_path / "by_design.yaml"
    bad.write_text(
        "version: 1\nsuppress:\n  - id: oops\n    owner: by-design\n"
        "    match:\n      paths: ['**/x.py']\n",
        encoding="utf-8",
    )
    with pytest.raises(bd.ByDesignError, match="reason"):
        bd.load_by_design(bad)


# ---------------------------------------------------------------------------
# Over-suppression — the expensive direction
# ---------------------------------------------------------------------------


def test_a_guardrail_finding_is_never_suppressed() -> None:
    """The worst failure this module could have.

    A guardrail is a merge-blocking fact reported regardless of confidence.
    Dropping one silently turns a blocked PR into an approved one, so the
    filter short-circuits before any entry is consulted.
    """
    loaded = bd.load_by_design(DATA)
    finding = _f(evidence="@pytest.mark.asyncio", pattern_id="G1")
    assert loaded.match(finding) is not None, "precondition: this normally suppresses"
    finding.guardrail = "G1"
    assert loaded.match(finding) is None


def test_the_seam_suppression_does_not_leak_into_adjacent_packages() -> None:
    """`fnmatch` treats `*` as matching separators, so `**/execution/_temporal/**`
    would otherwise also match `execution/_temporal_shim/`. That would suppress
    a real adapter-boundary violation in a package nobody audited."""
    loaded = bd.load_by_design(DATA)
    shim = _f(
        pattern_id="DirectTemporalImport",
        file="application_sdk/execution/_temporal_shim/leak.py",
    )
    assert loaded.match(shim) is None


def test_security_findings_survive_the_tech_debt_suppression() -> None:
    """`common/utils.py` is a tracked dumping ground and structural findings
    there are noise — but the entry is scoped by category precisely so that a
    security defect in the same file is still reported."""
    loaded = bd.load_by_design(DATA)
    structural = _f(file="application_sdk/common/utils.py", category="structure")
    security = _f(file="application_sdk/common/utils.py", category="security")
    assert loaded.match(structural) is not None
    assert loaded.match(security) is None


def test_a_substantiated_hot_path_survives_the_threadpool_suppression() -> None:
    """`unless_evidence` inverts the burden for the threshold entries.

    retro-log states these with a condition the runner cannot measure ("only
    flag if the path is hot"). Rather than drop the condition or trust the
    model to self-police, the pattern is suppressed by default and survives
    only when the reviewer showed its working.
    """
    loaded = bd.load_by_design(DATA)
    cold = _f(evidence="with ThreadPoolExecutor() as pool:")
    hot = _f(
        evidence="with ThreadPoolExecutor() as pool:",
        attack_path="called per row in the extraction loop, 5000+ calls per run",
    )
    assert loaded.match(cold) is not None
    assert loaded.match(hot) is None


def test_substantiation_is_read_from_evidence_or_attack_path() -> None:
    """Reviewers put the justification in whichever field fits the sentence.
    Searching only one would make suppression depend on prose placement."""
    loaded = bd.load_by_design(DATA)
    in_evidence = _f(evidence="run_in_thread(...) with heartbeat_timeout_seconds=30")
    in_attack = _f(
        evidence="run_in_thread(...)",
        attack_path="the task sets heartbeat_timeout_seconds",
    )
    assert loaded.match(in_evidence) is None
    assert loaded.match(in_attack) is None


# ---------------------------------------------------------------------------
# Under-suppression — the patterns that must actually be dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "finding,why",
    [
        (_f(pattern_id="G004"), "logging f-string — ruff G004 blocks it"),
        (_f(pattern_id="T201"), "print() — ruff T201 blocks it"),
        (_f(pattern_id="F401"), "unused import — ruff F401 blocks it"),
        (_f(pattern_id="UnpinnedActionReference"), "BLOCK-tier conformance rule"),
        (
            _f(evidence="@pytest.mark.asyncio\nasync def test_x():"),
            "settled team preference",
        ),
        (
            _f(
                file="application_sdk/infrastructure/_dapr/client.py",
                evidence="from dapr.clients import DaprClient",
            ),
            "the Dapr seam is the abstraction layer",
        ),
        (
            _f(
                evidence=(
                    "consider an alternative error-code scheme instead of "
                    "AAF-STORAGE-001 / NonRetryableError"
                )
            ),
            "scheme-proposal claim, not the identifier",
        ),
    ],
)
def test_known_non_findings_are_dropped(finding: Finding, why: str) -> None:
    assert bd.load_by_design(DATA).match(finding) is not None, why


def test_naming_credential_ref_is_not_enough_to_suppress() -> None:
    """Token-not-claim: quoting CredentialResolver is how a real finding is written.

    The short-circuit does not save this — credential-resolve-outside-task is
    not a guardrail. Matching the type name would drop it.
    """
    loaded = bd.load_by_design(DATA)
    real = _f(
        pattern_id="credential-resolve-outside-task",
        evidence="CredentialResolver.resolve() called in run()",
    )
    recommend = _f(evidence="use CredentialRef instead of a raw secret in the contract")
    false_positive = _f(
        evidence="CredentialRef treated as a secret in a Temporal payload"
    )
    assert loaded.match(real) is None
    assert loaded.match(recommend) is None
    assert loaded.match(false_positive) is not None


@pytest.mark.parametrize(
    "evidence",
    [
        # A recommendation TO use the ref. It names `secret` and `payload`
        # because those describe the defect it replaces, so neither word can
        # carry the match.
        "use CredentialRef instead of a raw secret in the Temporal payload",
        # Asserts the OPPOSITE of the false-positive claim, so a bare
        # "is a credential" alternative cannot carry it either.
        "CredentialRef is a credential reference (no actual secrets)",
    ],
)
def test_credential_ref_sentences_that_are_not_the_claim_survive(evidence: str) -> None:
    """The claim is "the ref itself is secret material" — nothing weaker.

    An earlier revision matched `secret` near `payload` and a bare
    "is a credential", which dropped both of these real findings.
    """
    assert bd.load_by_design(DATA).match(_f(evidence=evidence)) is None


def test_quoting_an_error_class_from_a_real_defect_is_not_suppressed() -> None:
    """review-policy forbids proposing a new hierarchy, not quoting the existing one."""
    loaded = bd.load_by_design(DATA)
    real = _f(evidence='raise NonRetryableError("AAF-STORAGE-001")')
    proposal = _f(
        evidence="consider an alternative error-code scheme instead of NonRetryableError"
    )
    assert loaded.match(real) is None
    assert loaded.match(proposal) is not None


@pytest.mark.parametrize(
    "evidence",
    [
        # An adjective next to the noun is not a proposal: this one reports a
        # mis-routed leaf, and matching `new` + `error hierarch` suppressed it.
        "the new error hierarchy routes storage failures to a retryable leaf",
        # Wrong-leaf findings say "instead of" about the CLASSES, not about the
        # scheme, so the comparative branch must not reach them either.
        "raise RetryableError instead of NonRetryableError for a permanent failure",
        "the error hierarchy is not applied consistently in this handler",
    ],
)
def test_error_scheme_defects_are_not_scheme_proposals(evidence: str) -> None:
    """Only a proposal to REPLACE the scheme is by-design; defects in it are real."""
    assert bd.load_by_design(DATA).match(_f(evidence=evidence)) is None


@pytest.mark.parametrize(
    "evidence",
    [
        # The claim this entry exists to suppress. The previous regex wanted
        # "alternative|new|…" and "error hierarch", so "custom" + "exception
        # hierarchy" walked straight past it.
        "consider using a custom exception hierarchy instead of NonRetryableError",
        "suggest a deeper error hierarchy",
        "propose an additional error class for storage failures",
    ],
)
def test_scheme_proposals_are_dropped_however_they_are_phrased(evidence: str) -> None:
    """Paraphrases of the proposal are the same non-finding, so all must drop."""
    assert bd.load_by_design(DATA).match(_f(evidence=evidence)) is not None


def test_meaningful_coverage_commentary_is_not_the_raw_threshold_claim() -> None:
    """`coverage` near a percentage is how a 'tests are meaningful?' finding is written."""
    loaded = bd.load_by_design(DATA)
    meaningful = _f(evidence="0% coverage of the new branch")
    threshold = _f(evidence="coverage dropped to 80, below fail_under=85")
    assert loaded.match(meaningful) is None
    assert loaded.match(threshold) is not None


def test_lowering_the_coverage_gate_is_not_the_threshold_claim() -> None:
    """CI enforces `fail_under`; it cannot notice the PR *lowering* it.

    So a finding that names `fail_under` to report the gate being weakened is
    the reviewer's to make, and a bare `fail_under` alternative ate it.
    """
    loaded = bd.load_by_design(DATA)
    assert loaded.match(_f(evidence="fail_under lowered from 85 to 70")) is None
    assert loaded.match(_f(evidence="the PR lowers fail_under from 85 to 70")) is None


def test_warn_tier_rules_are_still_reported_outside_their_seam() -> None:
    """The reviewer is the only enforcement for these. If the filter ate them
    the lane would silently stop checking the adapter boundary entirely."""
    loaded = bd.load_by_design(DATA)
    for rule in (
        "MissingExceptionChaining",
        "OrjsonOverStdlibJson",
        "LoggerCriticalUsage",
    ):
        assert loaded.match(_f(pattern_id=rule)) is None, f"{rule} was suppressed"


# ---------------------------------------------------------------------------
# Integration with the renderer
# ---------------------------------------------------------------------------


def test_normalise_records_which_entry_dropped_a_finding() -> None:
    """Auditability is the property that makes machine suppression safer than
    asking a model to stay quiet: every drop names the rule that caused it, so
    over-suppression is discoverable instead of invisible."""
    sev = load_severity()
    result = normalise([_f(pattern_id="T201")], sev, by_design=bd.load_by_design(DATA))
    assert not result.kept
    assert len(result.dropped) == 1
    assert "ci-logging-hygiene" in result.dropped[0].reason
    assert "ci-gate" in result.dropped[0].reason


def test_normalise_without_a_filter_is_unchanged() -> None:
    """The parameter is optional so #3604's contract still holds for callers
    that have not adopted the filter — including the pr-review lane's tests."""
    sev = load_severity()
    result = normalise([_f(pattern_id="T201")], sev)
    assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# A guardrail cannot be lost on the way to the verdict either
# ---------------------------------------------------------------------------


def test_an_under_rated_guardrail_still_blocks() -> None:
    """The suppression route the by-design filter does not cover.

    `normalise` used to route findings to `kept` or `prose` on severity alone,
    and the pattern clamp only ever LOWERS. So a model that emitted a guardrail
    pattern at LOW — a determinism violation it under-rated — put it in
    `prose`, where `compute_verdict` never looks. The guardrail's BLOCKED
    verdict never fired and a merge-blocking fact rendered as a passing remark.

    Same failure this module exists to prevent, one layer up: a guardrail
    silently not counting.
    """
    sev = load_severity()
    guarded = next(
        (p for entry in sev.guardrails.values() for p in entry["patterns"]), None
    )
    assert guarded, "severity.yaml defines no guardrail patterns"

    finding = _f(pattern_id=guarded, severity="LOW", confidence=0.1)
    result = normalise([finding], sev, by_design=bd.load_by_design(DATA))

    assert result.kept == [finding], "the guardrail was routed to prose"
    assert not result.prose
    assert sev.in_findings(
        finding.severity
    ), "the guardrail was kept at a severity that does not render into Findings"
    assert compute_verdict(result.kept, sev) == "BLOCKED"


def test_the_guardrail_floor_is_the_mildest_blocking_severity() -> None:
    """Floored, not escalated. The model's rating is evidence about how bad the
    defect is; the guardrail decides only that it counts. Promoting an
    under-rated finding straight to BLOCKING would overstate it."""
    sev = load_severity()
    assert sev.lowest_blocking() == "MEDIUM"
    assert sev.in_findings("MEDIUM") and not sev.in_findings("LOW")


def test_a_verdict_that_legitimately_has_no_findings_is_not_a_violation() -> None:
    """`NEEDS_REBASE` and `NEEDS_HUMAN` are both decided before any finding
    exists — one from mergeStateStatus, one from what the reviewer could not
    determine. Flagging them reported every rebase round as a contract
    violation, contaminating the measurement the audit exists to produce."""
    for verdict in ("NEEDS_REBASE", "NEEDS_HUMAN"):
        body = (
            "<!-- SDK_REVIEW -->\n"
            f"<!-- VERDICT: {verdict} -->\n"
            f"<!-- REVIEWED_HEAD: {'a' * 40} -->\n"
            "### Findings\n\n"
        )
        problems = [p for p in audit_comment(body) if "Findings is empty" in p]
        assert not problems, f"{verdict} was reported as a violation: {problems}"


def test_an_empty_findings_list_with_needs_fixes_is_still_a_violation() -> None:
    """The exemption is narrow on purpose: NEEDS_FIXES with nothing to fix
    would leave the resolve loop with no work and no way to terminate."""
    body = (
        "<!-- SDK_REVIEW -->\n"
        "<!-- VERDICT: NEEDS_FIXES -->\n"
        f"<!-- REVIEWED_HEAD: {'a' * 40} -->\n"
        "### Findings\n\n"
    )
    assert any("Findings is empty" in p for p in audit_comment(body))

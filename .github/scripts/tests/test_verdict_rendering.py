"""The frozen verdict-comment contract, now owned by Python.

`sdk_review_approve.py`, `sdk_review_reconcile.py`,
`sdk_review_dedupe_verdicts.py`, `sdk_review_verdict_gate.py` and four
`sdk-review-*.yml` workflows all find their work by parsing HTML comment
markers out of PR comments. Until now that contract was a 7.2 KB markdown
template the reviewer was asked to fill in by hand, and the playbook carries
two `sed` safety nets for the two ways it got that wrong: writing the literal
placeholder `<HEAD_SHA>` instead of a sha, and emitting an empty
`<!-- ANSWERS_TRIGGER: -->` on a `workflow_dispatch` run.

This module is where that contract lives now. Every assertion here is a
downstream consumer's parse, so a failure means a real PR would have gone
unlabelled, unapproved, or been read by the resolver as answering the wrong
round.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_phase  # noqa: E402
import sdk_review_approve as approve  # noqa: E402
from sdk_loop_common import (  # noqa: E402
    ALL_VERDICTS,
    MARK_VERDICT_BLOCK,
    parse_reviewed_head,
    parse_verdict,
)
from sdk_loop_findings import (  # noqa: E402
    SchemaError,
    audit_comment,
    compute_verdict,
    load_severity,
    normalise,
    parse_finding,
    render_markers,
    render_summary,
)

DATA = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".mothership/pr-loop/data/severity.yaml"
)
HEAD = "24ff8e453fd023731a7bb7032694e202997a2c70"


@pytest.fixture
def sev():
    return load_severity(DATA)


def _finding(**over):
    base = {
        "title": "VERSION_RE never matches an indented PklProject version line",
        "severity": "HIGH",
        "confidence": 0.9,
        "file": ".github/scripts/release_guard.py",
        "line": 56,
        "category": "CI",
        "suggested_fix": "anchor the regex with `^\\s*`",
    }
    base.update(over)
    return parse_finding(base)


# --------------------------------------------------------------------------
# Markers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ALL_VERDICTS)
def test_every_verdict_token_round_trips(verdict: str) -> None:
    """The five tokens the approval workflows accept, parsed back by their parser."""
    block = render_markers(verdict, HEAD)
    assert MARK_VERDICT_BLOCK in block
    assert parse_verdict(block) == verdict
    assert parse_reviewed_head(block) == HEAD


def test_reviewed_head_must_be_a_real_sha() -> None:
    """The `<HEAD_SHA>` placeholder class, deleted rather than sed-repaired.

    §3f carries a `sed` net because the reviewer sometimes wrote the literal
    placeholder. A caller cannot make that mistake here — and if it passes a
    short sha or a branch name, it fails loudly instead of producing a comment
    whose delta base the next round cannot resolve.
    """
    for bogus in ["<HEAD_SHA>", "24ff8e4", "", "main", HEAD.upper()]:
        with pytest.raises(SchemaError):
            render_markers("NEEDS_FIXES", bogus)


def test_answers_trigger_is_omitted_not_emptied_on_dispatch() -> None:
    """`COMMENT_ID` is blank on every `workflow_dispatch` run.

    The resolver's push guard uses this marker to tell "the round I am waiting
    on has answered" from "an earlier round's verdict landed late". An empty
    marker reads as present-but-unparseable, which is worse than a missing one:
    it can clear a push while the review is still running and strand the verdict.
    """
    for blank in (None, "", "   "):
        block = render_markers("NEEDS_FIXES", HEAD, answers_trigger=blank)
        assert "ANSWERS_TRIGGER" not in block

    block = render_markers("NEEDS_FIXES", HEAD, answers_trigger="5491746224")
    assert "<!-- ANSWERS_TRIGGER: 5491746224 -->" in block

    with pytest.raises(SchemaError):
        render_markers("NEEDS_FIXES", HEAD, answers_trigger="comment-5491746224")


def test_toolkit_hash_only_when_given() -> None:
    assert "TOOLKIT_ARTIFACT_HASH" not in render_markers("NEEDS_FIXES", HEAD)
    block = render_markers("NEEDS_FIXES", HEAD, toolkit_artifact_hash="ab" * 32)
    assert f"<!-- TOOLKIT_ARTIFACT_HASH: {'ab' * 32} -->" in block


def test_the_marker_block_leads_the_comment(sev) -> None:
    """Consumers scan from the top; the marker block must be the first bytes."""
    body = render_summary(
        verdict="READY_TO_MERGE",
        pr_number=3580,
        pr_title="ci(release): guard every release opener",
        reviewed_head=HEAD,
        kept=[],
        sev=sev,
        summary="Nothing outstanding.",
    )
    assert body.startswith(MARK_VERDICT_BLOCK + "\n")


# --------------------------------------------------------------------------
# The termination invariant
# --------------------------------------------------------------------------


def test_empty_findings_means_ready_to_merge(sev) -> None:
    """The resolve loop's termination condition, as an invariant.

    `@sdk-resolve` loops review -> fix -> push until `### Findings` is empty,
    and READY_TO_MERGE requires that same empty list. Making this a renderer
    property rather than an instruction is the whole reason verdict assembly
    moved out of the model.
    """
    assert compute_verdict([], sev) == "READY_TO_MERGE"
    body = render_summary(
        verdict="READY_TO_MERGE",
        pr_number=3586,
        pr_title="chore(conformance): I005 false-positive shapes",
        reviewed_head=HEAD,
        kept=[],
        sev=sev,
        summary="All prior findings resolved.",
    )
    section = body.split("### Findings", 1)[1]
    assert "_None._" in section
    assert parse_verdict(body) == "READY_TO_MERGE"


def test_any_rendered_finding_forces_needs_fixes(sev) -> None:
    """Strict, and strict at every tier — a lone Nit does it too."""
    for severity in ("MEDIUM", "HIGH", "CRITICAL"):
        kept = normalise([_finding(severity=severity, confidence=0.95)], sev).kept
        assert kept, severity
        assert compute_verdict(kept, sev) == "NEEDS_FIXES", severity


def test_prose_only_tiers_do_not_block(sev) -> None:
    """LOW/INFO reach the summary but never `### Findings`, so they never block."""
    result = normalise(
        [_finding(severity="LOW", confidence=0.9), _finding(severity="INFO")], sev
    )
    assert result.kept == []
    assert len(result.prose) == 2
    assert compute_verdict(result.kept, sev) == "READY_TO_MERGE"


# --------------------------------------------------------------------------
# Guardrails, clamping, floors
# --------------------------------------------------------------------------


def test_a_guardrail_pattern_forces_its_verdict(sev) -> None:
    kept = normalise(
        [_finding(pattern_id="credential-in-log", severity="BLOCKING")], sev
    ).kept
    assert compute_verdict(kept, sev) == "BLOCKED"


def test_severity_is_clamped_down_to_max(sev) -> None:
    """The model may under- or over-call; `max_severity` is the ceiling."""
    kept = normalise(
        [_finding(pattern_id="missing-type-annotation", severity="BLOCKING")], sev
    ).kept
    assert kept[0].severity == "MEDIUM"
    assert kept[0].tier == "Nit"


def test_the_floor_is_applied_after_clamping(sev) -> None:
    """Clamp first, then floor — the floor belongs to the tier it ends up in.

    A finding the model called CRITICAL at 0.60 confidence, on a pattern capped
    at MEDIUM, is a valid MEDIUM (floor 0.55). Checking the floor first would
    hold it to CRITICAL's 0.85 and drop a real finding.
    """
    result = normalise(
        [
            _finding(
                pattern_id="missing-type-annotation",
                severity="CRITICAL",
                confidence=0.6,
            )
        ],
        sev,
    )
    assert result.kept and not result.dropped
    assert result.kept[0].severity == "MEDIUM"


def test_low_confidence_is_dropped_with_a_reason(sev) -> None:
    result = normalise([_finding(severity="HIGH", confidence=0.5)], sev)
    assert not result.kept
    assert "below the HIGH floor" in result.dropped[0].reason


def test_a_guardrail_ignores_the_confidence_floor(sev) -> None:
    """`CLAUDE.md`: guardrail violations are reported regardless of confidence."""
    result = normalise(
        [_finding(pattern_id="field-removed", severity="BLOCKING", confidence=0.1)], sev
    )
    assert result.kept, "a guardrail was dropped by a confidence floor"
    assert compute_verdict(result.kept, sev) == "BLOCKED"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_fields_the_handler_422s_on_are_refused() -> None:
    """All seven brief-level JSON examples emit exactly these three."""
    for bad in ("scope", "domain_tag", "guardrail", "public_note"):
        with pytest.raises(SchemaError, match="422"):
            parse_finding(
                {
                    "title": "x",
                    "severity": "HIGH",
                    "confidence": 0.9,
                    "file": "a.py",
                    bad: "whatever",
                }
            )


def test_the_payload_only_carries_allowlisted_fields(sev) -> None:
    finding = _finding()
    normalise([finding], sev)
    assert set(finding.payload()) <= {
        "title",
        "pattern_id",
        "severity",
        "category",
        "confidence",
        "file",
        "line",
        "evidence",
        "attack_path",
        "reachable_from",
        "by_design_check",
        "suggested_fix",
        "escalate_to_linear",
    }
    assert "tier" not in finding.payload()
    assert "guardrail" not in finding.payload()


@pytest.mark.parametrize(
    "raw",
    [
        {"severity": "HIGH", "confidence": 0.9, "file": "a.py"},
        {"title": "x", "confidence": 0.9, "file": "a.py"},
        {"title": "x", "severity": "HIGH", "confidence": "high", "file": "a.py"},
        {"title": "x", "severity": "HIGH", "confidence": 1.4, "file": "a.py"},
    ],
)
def test_malformed_findings_raise(raw: dict) -> None:
    with pytest.raises(SchemaError):
        parse_finding(raw)


# --------------------------------------------------------------------------
# Rendering shape
# --------------------------------------------------------------------------


def test_findings_are_grouped_by_file_and_sorted_by_tier(sev) -> None:
    """§3e's mandatory format: file-grouped bullets, never a markdown table."""
    kept = normalise(
        [
            _finding(file="z.py", severity="MEDIUM", line=10, title="nit"),
            _finding(file="a.py", severity="HIGH", line=99, title="important"),
            _finding(
                file="a.py",
                severity="BLOCKING",
                line=5,
                title="critical",
                pattern_id="credential-in-log",
            ),
        ],
        sev,
    ).kept
    body = render_summary(
        verdict=compute_verdict(kept, sev),
        pr_number=1,
        pr_title="t",
        reviewed_head=HEAD,
        kept=kept,
        sev=sev,
        summary="s",
    )
    section = body.split("### Findings", 1)[1]
    assert section.index("**`a.py`**") < section.index("**`z.py`**")
    assert section.index("**Critical**") < section.index("**Important**")
    assert "| Severity |" not in section, "rendered a table; §3e forbids it"


def test_rereview_titles_itself_a_rereview(sev) -> None:
    body = render_summary(
        verdict="READY_TO_MERGE",
        pr_number=1,
        pr_title="t",
        reviewed_head=HEAD,
        kept=[],
        sev=sev,
        summary="s",
        is_rereview=True,
        delta="- **Resolved (1)**: the regex anchor",
    )
    assert "Re-review" in body
    assert "### Delta from previous review" in body


def test_toolkit_scope_keeps_the_stable_marker(sev) -> None:
    """The heading changes; `<!-- SDK_REVIEW -->` must not."""
    body = render_summary(
        verdict="NEEDS_HUMAN",
        pr_number=3594,
        pr_title="t",
        reviewed_head=HEAD,
        kept=[],
        sev=sev,
        summary="s",
        heading_subject="Contract Toolkit",
    )
    assert "## Contract Toolkit Review" in body
    assert MARK_VERDICT_BLOCK in body
    assert parse_verdict(body) == "NEEDS_HUMAN"


def test_conflicting_short_circuits_to_needs_rebase(sev) -> None:
    """Decided from mergeStateStatus with no model call, so no finding outvotes it."""
    kept = normalise(
        [_finding(severity="BLOCKING", pattern_id="field-removed")], sev
    ).kept
    assert compute_verdict(kept, sev, conflicting=True) == "NEEDS_REBASE"


# --------------------------------------------------------------------------
# Round-trip against the merge authority itself
# --------------------------------------------------------------------------


def test_the_rendered_comment_parses_in_sdk_review_approve(sev) -> None:
    """The strongest assertion in this module: the real approval path reads it.

    Neither lane is the merge authority. `sdk_review_approve.py` is — it casts
    the `atlan-ci` APPROVE, and `atlan-ci` is the CODEOWNER whose review
    satisfies the branch ruleset on `main`. So the contract that matters is not
    "sdk_loop_common can parse this", it is "the script that approves can".
    Those are different modules with independently-written regexes.
    """
    for verdict in ALL_VERDICTS:
        body = render_summary(
            verdict=verdict,
            pr_number=3587,
            pr_title="chore(conformance): P025 false-positive shapes",
            reviewed_head=HEAD,
            kept=[],
            sev=sev,
            summary="s",
            answers_trigger="5491746224",
        )
        assert any(m in body for m in approve.SUMMARY_MARKERS)
        assert approve.VERDICT_RE.search(body).group(1) == verdict
        assert approve.REVIEWED_HEAD_RE.search(body).group(1) == HEAD


def test_the_rendered_comment_matches_a_real_posted_verdict(sev) -> None:
    """Shape-check against a verdict `@sdk-loop` actually posted on PR #3580.

    Marker order and spelling are copied from that comment verbatim. If the
    renderer ever reorders or respaces them, downstream regexes would still
    match — but the diff against the old lane's output would stop being
    reviewable by eye, which is how the shadow comparison in the review phase
    earns its keep.
    """
    real_prefix = [
        "<!-- SDK_REVIEW -->",
        "<!-- VERDICT: NEEDS_FIXES -->",
        f"<!-- REVIEWED_HEAD: {HEAD} -->",
        "<!-- ANSWERS_TRIGGER: 5491269936 -->",
    ]
    kept = normalise([_finding()], sev).kept
    body = render_summary(
        verdict=compute_verdict(kept, sev),
        pr_number=3580,
        pr_title="ci(release): guard every release opener",
        reviewed_head=HEAD,
        kept=kept,
        sev=sev,
        summary="s",
        answers_trigger="5491269936",
    )
    assert body.splitlines()[:4] == real_prefix


# --------------------------------------------------------------------------
# The shadow audit
# --------------------------------------------------------------------------


def test_the_auditor_passes_a_well_formed_comment(sev) -> None:
    kept = normalise([_finding()], sev).kept
    body = render_summary(
        verdict=compute_verdict(kept, sev),
        pr_number=3580,
        pr_title="t",
        reviewed_head=HEAD,
        kept=kept,
        sev=sev,
        summary="s",
        answers_trigger="5491269936",
    )
    assert audit_comment(body) == []


def test_the_auditor_passes_an_empty_findings_approval(sev) -> None:
    body = render_summary(
        verdict="READY_TO_MERGE",
        pr_number=3586,
        pr_title="t",
        reviewed_head=HEAD,
        kept=[],
        sev=sev,
        summary="s",
    )
    assert audit_comment(body) == []


@pytest.mark.parametrize(
    "mutate, expect",
    [
        # The two failures §3f carries a `sed` for.
        (lambda b: b.replace(HEAD, "<HEAD_SHA>"), "literal placeholder"),
        (
            lambda b: b.replace(
                "<!-- ANSWERS_TRIGGER: 5491269936 -->", "<!-- ANSWERS_TRIGGER: -->"
            ),
            "present but empty",
        ),
        # A verdict that would approve a PR with open findings.
        (
            lambda b: b.replace(
                "<!-- VERDICT: NEEDS_FIXES -->", "<!-- VERDICT: READY_TO_MERGE -->"
            ),
            "would approve a PR with open findings",
        ),
        # Markers the consumers key on, removed.
        (lambda b: b.replace("<!-- SDK_REVIEW -->\n", ""), "no <!-- SDK_REVIEW -->"),
        (lambda b: b.replace("<!-- VERDICT: NEEDS_FIXES -->\n", ""), "no <!-- VERDICT"),
        (lambda b: re.sub(r"<!-- REVIEWED_HEAD:[^>]*-->\n", "", b), "no REVIEWED_HEAD"),
        # A short sha still parses in sdk_loop_common but loses the delta base.
        (lambda b: b.replace(HEAD, HEAD[:7]), "not a 40-char lowercase sha"),
        # An invented sixth verdict token.
        (
            lambda b: b.replace(
                "<!-- VERDICT: NEEDS_FIXES -->", "<!-- VERDICT: LGTM -->"
            ),
            "not one of the five",
        ),
        # Prose before the markers pushes them out of the leading position.
        (lambda b: "Here is my review!\n\n" + b, "does not lead the comment"),
        # §3e forbids the table form outright.
        (
            lambda b: b.replace(
                "### Findings", "### Findings\n\n| Severity | Where |\n|---|---|"
            ),
            "rendered as a table",
        ),
    ],
)
def test_the_auditor_catches_each_known_break(sev, mutate, expect: str) -> None:
    """A checker that passes everything is worth nothing.

    Every row is a real failure mode: the two the playbook's `sed` nets exist
    for, the marker removals that make a verdict invisible to
    `sdk_review_approve.py`, and the Findings/verdict disagreement that would
    approve a PR with open findings.
    """
    kept = normalise([_finding()], sev).kept
    body = render_summary(
        verdict=compute_verdict(kept, sev),
        pr_number=3580,
        pr_title="t",
        reviewed_head=HEAD,
        kept=kept,
        sev=sev,
        summary="s",
        answers_trigger="5491269936",
    )
    problems = audit_comment(mutate(body))
    assert any(expect in p for p in problems), (expect, problems)


def test_findings_emptiness_reads_prose_as_empty(sev) -> None:
    """`_None._`, `None`, and an absent section all mean no findings."""
    for spelling in ("_None._", "None", "No findings.", ""):
        body = (
            f"<!-- SDK_REVIEW -->\n<!-- VERDICT: READY_TO_MERGE -->\n"
            f"<!-- REVIEWED_HEAD: {HEAD} -->\n## t\n\n### Findings\n\n{spelling}\n\n"
            f"### Strengths\n- ok\n"
        )
        assert audit_comment(body) == [], spelling


def test_the_review_phase_audits_the_comment_without_failing_the_round() -> None:
    """The audit is a diagnostic, not a gate — at this step.

    Failing a round on a contract violation would make the measurement cost a
    review, and the point of running the renderer behind the playbook is to
    find out how often the model breaks the contract BEFORE anything depends
    on it not breaking. The outcome must be decided by `interpret_review`
    alone.
    """
    broken = {
        "body": (
            "<!-- SDK_REVIEW -->\n<!-- VERDICT: READY_TO_MERGE -->\n"
            "<!-- REVIEWED_HEAD: <HEAD_SHA> -->\n<!-- ANSWERS_TRIGGER: -->\n"
            "## t\n\n### Findings\n\n- **Critical** [SEC] L1 — boom.\n"
        )
    }
    problems = sdk_loop_phase.audit_verdict_contract(broken)
    assert any("literal placeholder" in p for p in problems)
    assert any("present but empty" in p for p in problems)
    assert any("would approve a PR with open findings" in p for p in problems)

    assert sdk_loop_phase.audit_verdict_contract(None) == []

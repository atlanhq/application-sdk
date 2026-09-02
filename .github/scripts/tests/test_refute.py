"""Cross-examination, and the four ways it could delete a real defect.

Almost every test here is about the arbiter failing *open*. A refutation stage
that drops findings when it breaks is worse than no refutation stage, because
the failure is invisible: the review reports less and looks cleaner. Suppression
has to be something the refuter did deliberately and argued for.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_refute as rf  # noqa: E402
from sdk_loop_findings import Finding, load_severity  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
BRIEF = REPO / ".mothership/pr-loop/REFUTE.md"

ARGUED = "The branch is unreachable: `strict` is never set on this path, and I checked all four callers."


def _f(
    title="a bug", severity="HIGH", file="application_sdk/x.py", line=10, **kw
) -> Finding:
    return Finding(
        title=title, severity=severity, confidence=0.9, file=file, line=line, **kw
    )


def _challenge(finding, stance, reason="", severity=None) -> rf.Challenge:
    return rf.Challenge(
        target=rf.finding_key(finding), stance=stance, reason=reason, severity=severity
    )


@pytest.fixture
def sev():
    return load_severity()


# ---------------------------------------------------------------------------
# Identity across the boundary
# ---------------------------------------------------------------------------


def test_findings_are_matched_by_content_not_list_index() -> None:
    """The refuter returns free-form JSON and reorders, drops and merges
    entries. An index-keyed match silently applies one finding's verdict to
    another — a real defect deleted by an argument written about something
    else, which is the worst outcome this module has available."""
    a, b = _f(title="first"), _f(title="second", line=99)
    assert rf.finding_key(a) != rf.finding_key(b)
    assert rf.finding_key(a) == rf.finding_key(_f(title="first"))


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------


def test_a_finding_with_no_verdict_is_kept(sev) -> None:
    result = rf.arbitrate([_f()], [], sev)
    assert len(result.kept) == 1 and not result.dropped


def test_an_unargued_disagreement_does_not_drop_anything(sev) -> None:
    """A bare "false positive" is a vote. Votes are not reviewable later by the
    person wondering where a finding went."""
    finding = _f()
    result = rf.arbitrate([finding], [_challenge(finding, rf.DISAGREE, "nope")], sev)
    assert result.kept == [finding]
    assert not result.dropped


def test_an_argued_disagreement_drops_and_records_why(sev) -> None:
    finding = _f()
    result = rf.arbitrate([finding], [_challenge(finding, rf.DISAGREE, ARGUED)], sev)
    assert not result.kept
    assert result.dropped[0][1] == ARGUED


def test_unparseable_output_challenges_nothing(sev) -> None:
    """A refuter that times out mid-sentence must not be read as agreement to
    delete."""
    assert rf.parse_challenges("not json at all") == ()
    assert rf.parse_challenges('{"challenges": "wrong shape"}') == ()


def test_an_unknown_stance_is_dropped_not_guessed(sev) -> None:
    """A stance this module does not recognise means the refuter answered a
    different question. Mapping it onto one of ours invents an opinion nobody
    expressed."""
    parsed = rf.parse_challenges(
        '{"challenges": [{"target": "x", "stance": "MAYBE", "reason": "hmm"}]}'
    )
    assert parsed == ()


def test_a_verdict_for_a_finding_that_does_not_exist_is_reported(sev) -> None:
    """Silently ignoring it would hide a refuter that is answering about a
    different review entirely."""
    stray = rf.Challenge(target="ghost.py:1:nothing", stance=rf.DISAGREE, reason=ARGUED)
    result = rf.arbitrate([_f()], [stray], sev)
    assert result.unmatched == ["ghost.py:1:nothing"]
    assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# The rules that cost the most if wrong
# ---------------------------------------------------------------------------


def test_a_guardrail_finding_cannot_be_challenged_away(sev) -> None:
    """A guardrail is a merge-blocking fact reported regardless of confidence.
    If one fires wrongly that is a rubric bug — not something a challenger
    votes away."""
    finding = _f()
    finding.guardrail = "G3"
    result = rf.arbitrate([finding], [_challenge(finding, rf.DISAGREE, ARGUED)], sev)
    assert result.kept == [finding]
    assert not result.dropped


def test_partial_can_lower_severity(sev) -> None:
    finding = _f(severity="CRITICAL")
    result = rf.arbitrate(
        [finding], [_challenge(finding, rf.PARTIAL, ARGUED, severity="MEDIUM")], sev
    )
    assert finding.severity == "MEDIUM"
    assert result.adjusted[0][1:] == ("CRITICAL", "MEDIUM")


def test_partial_cannot_raise_severity(sev) -> None:
    """The proposer read the code with the pack in front of it; the refuter is
    arguing from the finding. Letting the weaker context escalate would make
    the challenge a second, worse proposer."""
    finding = _f(severity="MEDIUM")
    rf.arbitrate(
        [finding], [_challenge(finding, rf.PARTIAL, ARGUED, severity="BLOCKING")], sev
    )
    assert finding.severity == "MEDIUM"


def test_an_unknown_severity_in_a_partial_is_ignored(sev) -> None:
    finding = _f(severity="HIGH")
    rf.arbitrate(
        [finding], [_challenge(finding, rf.PARTIAL, ARGUED, severity="SPICY")], sev
    )
    assert finding.severity == "HIGH"


def test_agree_keeps_the_finding_untouched(sev) -> None:
    finding = _f(severity="HIGH")
    result = rf.arbitrate([finding], [_challenge(finding, rf.AGREE, "checked it")], sev)
    assert result.kept == [finding] and finding.severity == "HIGH"


# ---------------------------------------------------------------------------
# Saying how hard the check actually was
# ---------------------------------------------------------------------------


def test_the_summary_says_which_kind_of_challenge_ran(sev) -> None:
    """A different family is the strong form and needs $PROXY_BASE, which
    @sdk-loop does not have. A same-family challenger is weaker but real. A
    summary that says "challenged" without saying how hard is worth less than
    one that admits the weak form, because the reader calibrates on it."""
    kept = rf.Arbitration(kept=[_f()], mode=rf.CROSS_FAMILY)
    weak = rf.Arbitration(kept=[_f()], mode=rf.SAME_FAMILY)
    assert "different model family" in rf.render(kept)
    assert "same family" in rf.render(weak)


def test_a_lane_with_no_second_reviewer_says_so(sev) -> None:
    """Not a defect to report — a capability the lane lacks. But the reader
    must know the findings were not challenged."""
    text = rf.render(rf.Arbitration(kept=[_f()], mode=rf.NOT_RUN))
    assert "not run" in text
    assert "as first proposed" in text


def test_withdrawn_findings_are_listed_with_their_reasons(sev) -> None:
    """Otherwise a finding vanishes between rounds and the author cannot tell
    whether it was resolved or overruled."""
    finding = _f(title="the bug")
    result = rf.arbitrate([finding], [_challenge(finding, rf.DISAGREE, ARGUED)], sev)
    text = rf.render(result)
    assert "the bug" in text and "unreachable" in text


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


def _prose() -> str:
    """The brief with wrapping collapsed.

    Asserting on prose that is hard-wrapped at 79 columns otherwise fails on
    where a sentence happened to break, which says nothing about whether the
    rule is present.
    """
    return " ".join(BRIEF.read_text(encoding="utf-8").split())


def test_the_brief_forbids_raising_severity() -> None:
    assert "may not raise one" in _prose()


def test_the_brief_requires_an_argument_not_a_vote() -> None:
    assert "shorter than a sentence is discarded" in _prose()


def test_the_brief_separates_severity_disputes_from_disagreement() -> None:
    """Without this the refuter downgrades by disagreeing, and real findings
    disappear instead of being re-rated."""
    assert "Do not disagree because a finding is *minor*" in _prose()


def test_the_brief_does_not_point_at_the_old_corpus() -> None:
    text = BRIEF.read_text(encoding="utf-8")
    for forbidden in (
        "ORCHESTRATION.md",
        "pr-review/",
        "retro-log.md",
        "severity-rubric",
    ):
        assert forbidden not in text

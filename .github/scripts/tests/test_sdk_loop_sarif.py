"""Conformance coverage for a PR head — and the two ways it must not lie.

The whole value of wiring the deterministic gate into the review is that the
reviewer stops re-litigating rules a detector already decided. The whole RISK
is the inverse: telling the reviewer a surface is covered when nothing checked
it, so it skips the surface and nobody looks. Every test here is about keeping
those two apart.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_sarif import (  # noqa: E402
    STATE_COMPLETE,
    STATE_PARTIAL,
    STATE_PENDING,
    STATE_UNAVAILABLE,
    Coverage,
    Rule,
    classify_findings,
    load_catalog,
    render_section,
    run_for_head,
)

HEAD = "24ff8e453fd023731a7bb7032694e202997a2c70"


def _gh(payload, code=0):
    def fake(args):
        return code, json.dumps(payload) if payload is not None else ""

    return fake


# --------------------------------------------------------------------------
# The catalog
# --------------------------------------------------------------------------


def test_the_real_catalog_parses() -> None:
    """All 167 generated rule docs, read the way the runner will read them."""
    catalog = load_catalog()
    assert len(catalog) == 167, f"parsed {len(catalog)}"
    assert catalog["L001"].tier == "block"
    assert catalog["L001"].scope == "both"


def test_only_block_tier_sdk_binding_rules_may_suppress_prose() -> None:
    """The suppression set is 19 rules, not 167 — and that is the point.

    `retro-log.md` states it directly: conformance surfaces WARN findings but
    does not block, "so the reviewer is the only enforcement". And an
    `app`-scoped rule does not run against `application_sdk/**` at all, so for
    an SDK PR it is not coverage in any sense. Treating either as covered would
    silently delete the reviewer's largest real surface, which is exactly the
    failure this module exists to avoid.
    """
    catalog = load_catalog()
    suppressing = [r for r in catalog.values() if r.suppresses_prose]
    assert len(suppressing) == 19, (
        f"{len(suppressing)} rules claim to suppress reviewer prose; if the "
        "catalog genuinely changed, update the number deliberately — do not "
        "let it drift upward unnoticed"
    )
    assert all(r.tier == "block" for r in suppressing)
    assert all(r.scope in ("sdk", "both") for r in suppressing)


@pytest.mark.parametrize(
    "rule, expected",
    [
        (Rule("X", "block", "both"), True),
        (Rule("X", "block", "sdk"), True),
        (Rule("X", "block", "app"), False),  # never runs on the SDK
        (Rule("X", "warn", "both"), False),  # reported, not blocked
        (Rule("X", "warn", "sdk"), False),
    ],
)
def test_suppression_eligibility(rule: Rule, expected: bool) -> None:
    assert rule.suppresses_prose is expected


# --------------------------------------------------------------------------
# Finding the run for THIS commit
# --------------------------------------------------------------------------


def test_the_lookup_filters_server_side_by_commit() -> None:
    """The window bug, pinned.

    The first implementation pulled the last N gate runs and matched `headSha`
    locally. Probed against three real PR heads, a 40-run window reported
    `unavailable` for all three merged ones — indistinguishable from "the gate
    never ran" — while `gh run list --commit` found every one. A client-side
    window degrades exactly when the loop is several rounds deep and the head
    is no longer newest, which is when coverage matters most.
    """
    seen: list[list[str]] = []

    def gh(args):
        seen.append(args)
        return 0, json.dumps(
            [{"databaseId": 1, "status": "completed", "conclusion": "failure"}]
        )

    run_id, state = run_for_head("o/r", HEAD, gh=gh)
    assert (run_id, state) == (1, STATE_COMPLETE)
    assert (
        "--commit" in seen[0] and HEAD in seen[0]
    ), "the lookup must filter by commit server-side, not window-and-match"


def test_a_failing_gate_run_still_counts() -> None:
    """The suite reports findings by design, so `failure` carries real SARIF."""
    runs = [{"databaseId": 7, "status": "completed", "conclusion": "failure"}]
    assert run_for_head("o/r", HEAD, gh=_gh(runs))[1] == STATE_COMPLETE


def test_a_still_running_gate_is_pending_not_unavailable() -> None:
    """Different states, because a caller may wait for one and never the other."""
    runs = [{"databaseId": 9, "status": "in_progress", "conclusion": None}]
    assert run_for_head("o/r", HEAD, gh=_gh(runs)) == (9, STATE_PENDING)


def test_no_run_for_this_head_is_unavailable() -> None:
    """The common case on round N+1, straight after a resolve push."""
    assert run_for_head("o/r", HEAD, gh=_gh([])) == (None, STATE_UNAVAILABLE)


@pytest.mark.parametrize(
    "gh",
    [_gh(None, code=1), _gh(None), lambda args: (0, "not json")],
)
def test_a_broken_gh_call_degrades_to_unavailable(gh) -> None:
    """Never raise. A review must not fail because the gate lookup did.

    `unavailable` makes the reviewer do the work itself, which is the safe
    direction; raising would turn a CI hiccup into a lost round.
    """
    assert run_for_head("o/r", HEAD, gh=gh) == (None, STATE_UNAVAILABLE)


# --------------------------------------------------------------------------
# Classifying what fired
# --------------------------------------------------------------------------


MINE = "application_sdk/app/base.py"
THEIRS = "application_sdk/handler/service.py"


def _sarif(*pairs):
    """`(rule_id, path)` pairs -> a SARIF doc with real locations."""
    return {
        "s": {
            "runs": [
                {
                    "results": [
                        {
                            "ruleId": rule_id,
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": path}
                                    }
                                }
                            ],
                        }
                        for rule_id, path in pairs
                    ]
                }
            ]
        }
    }


def test_findings_split_by_tier_and_scope() -> None:
    catalog = {
        "L001": Rule("L001", "block", "both"),
        "E016": Rule("E016", "warn", "both"),
        "K006": Rule("K006", "block", "app"),
    }
    blocked, warned, elsewhere = classify_findings(
        _sarif(("L001", MINE), ("E016", MINE), ("K006", MINE)), catalog, [MINE]
    )
    assert blocked == ["L001"]
    assert warned == ["E016", "K006"]
    assert elsewhere == 0


def test_findings_outside_the_diff_never_suppress() -> None:
    """The bug real data caught, and the most dangerous one in the module.

    The suite scans the whole repo. On the gate run for one real PR there were
    584 results across 137 files and **zero** in the five files that PR
    touched. Unscoped, the pack would have said "CI already blocks E002, do not
    restate" — silencing the reviewer on a bare-except the PR genuinely
    introduced, because E002 fired somewhere else entirely. That loses a real
    finding, silently.
    """
    catalog = {"E002": Rule("E002", "block", "both")}
    blocked, warned, elsewhere = classify_findings(
        _sarif(("E002", THEIRS)), catalog, [MINE]
    )
    assert blocked == [], "a finding outside the diff must not suppress the reviewer"
    assert warned == []
    assert elsewhere == 1


def test_a_result_with_no_location_is_not_attributed() -> None:
    """Unattributable means "not this PR's", not "everywhere"."""
    catalog = {"L001": Rule("L001", "block", "both")}
    doc = {"s": {"runs": [{"results": [{"ruleId": "L001"}]}]}}
    blocked, warned, elsewhere = classify_findings(doc, catalog, [MINE])
    assert (blocked, warned, elsewhere) == ([], [], 1)


def test_an_unknown_rule_is_warned_never_blocked() -> None:
    """Guessing wrong toward "blocked" makes the reviewer skip a live surface.

    An id the catalog does not know cannot be proven to block anything, so it
    is passed through as context. The reviewer looking twice is cheap; the
    reviewer not looking at all is the bug.
    """
    blocked, warned, _ = classify_findings(_sarif(("Z999", MINE)), {}, [MINE])
    assert blocked == []
    assert warned == ["Z999"]


def test_duplicate_results_collapse() -> None:
    catalog = {"L001": Rule("L001", "block", "both")}
    blocked, _, _ = classify_findings(
        _sarif(("L001", MINE), ("L001", MINE), ("L001", MINE)), catalog, [MINE]
    )
    assert blocked == ["L001"]


def test_empty_sarif_yields_nothing() -> None:
    assert classify_findings({}, {}, [MINE]) == ([], [], 0)
    assert classify_findings({"s": {"runs": []}}, {}, [MINE]) == ([], [], 0)


def test_pre_existing_findings_are_reported_as_context_not_suppression() -> None:
    text = render_section(Coverage(state=STATE_COMPLETE, elsewhere=584))
    assert "584 further finding(s)" in text
    assert "suppress nothing here" in text
    assert "scoped to the files THIS PR changed" in text


# --------------------------------------------------------------------------
# What the reviewer is told
# --------------------------------------------------------------------------


def test_every_state_carries_an_explicit_instruction() -> None:
    """The reviewer must never infer what an empty findings list means.

    That inference is precisely how "the gate did not run" becomes "the gate
    found nothing", and a surface goes unreviewed by both.
    """
    for state in (STATE_COMPLETE, STATE_PARTIAL, STATE_PENDING, STATE_UNAVAILABLE):
        text = render_section(Coverage(state=state))
        assert f"**State: {state}." in text
        assert len(text.splitlines()) > 3, f"{state} rendered no instruction"


@pytest.mark.parametrize("state", [STATE_PENDING, STATE_UNAVAILABLE])
def test_absence_never_reads_as_clean(state: str) -> None:
    text = render_section(Coverage(state=state))
    assert "Assume no detector coverage" in text
    assert "Review every surface yourself" in text


def test_partial_names_the_unchecked_series() -> None:
    text = render_section(
        Coverage(
            state=STATE_PARTIAL,
            series_present=["logging"],
            series_missing=["tests", "security"],
        )
    )
    assert "unchecked" in text
    assert "`tests` — unchecked" in text
    assert "`security` — unchecked" in text


def test_blocked_and_warned_are_rendered_differently() -> None:
    """Warn-tier must never be presented as suppression."""
    text = render_section(
        Coverage(state=STATE_COMPLETE, blocked=["L001"], warned=["E016"])
    )
    assert "do not restate" in text
    assert "`L001`" in text
    assert "still yours to judge" in text
    assert "does **not** block" in text
    assert "`E016`" in text


def test_the_suppression_set_is_only_the_blocked_ids() -> None:
    cov = Coverage(state=STATE_COMPLETE, blocked=["L001", "P001"], warned=["E016"])
    assert cov.suppression_set == {"L001", "P001"}
    assert "E016" not in cov.suppression_set

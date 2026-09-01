"""The review playbook is split into a router plus conditional sections.

An orphaned section is SILENT. Nothing errors, no check goes red — the agent
simply never reads it, the review quietly loses whatever that file governed,
and the verdict comes back as confident as ever. That is the same failure shape
`review_subagents` already refuses to trust for `{file:...}` templates, and it
is why these invariants are asserted rather than assumed.
"""

from __future__ import annotations

import pathlib
import re

PLAYBOOK_DIR = pathlib.Path(__file__).resolve().parents[3] / ".mothership/pr-review"
ROUTER = PLAYBOOK_DIR / "ORCHESTRATION.md"
SECTIONS = PLAYBOOK_DIR / "sections"

#: Chars per token, near enough for a budget guard. The point of the number is
#: to fail when someone re-inlines a large block, not to be exact.
CHARS_PER_TOKEN = 4

#: What the router may cost before it stops being a router. Set above today's
#: measured size with room to grow, and far below the 20.6K it replaced.
ROUTER_TOKEN_CEILING = 16_000


def _router() -> str:
    return ROUTER.read_text(encoding="utf-8")


def _section_files() -> set[str]:
    return {p.name for p in SECTIONS.iterdir() if p.suffix == ".md"}


def _referenced() -> set[str]:
    return set(re.findall(r"sections/([a-z0-9-]+\.md)", _router()))


def test_every_section_is_reachable_from_the_router() -> None:
    """A section nobody points at is dead text that still has to be maintained,
    and its absence from a review is invisible."""
    orphaned = _section_files() - _referenced()
    assert not orphaned, (
        f"section file(s) not referenced from ORCHESTRATION.md: {sorted(orphaned)}. "
        "Add a pointer, or delete the file."
    )


def test_every_pointer_resolves_to_a_file() -> None:
    """A pointer to a missing file sends the agent to read nothing, and the
    Read simply fails mid-phase — after the turn has been paid for."""
    dangling = _referenced() - _section_files()
    assert (
        not dangling
    ), f"ORCHESTRATION.md points at missing section(s): {sorted(dangling)}"


def test_every_pointer_states_its_condition() -> None:
    """The whole saving is conditional loading. A pointer that does not say
    WHEN to read the file gets read every time, which is the state this split
    exists to leave — so each one must carry a condition the agent can evaluate.
    """
    # Everything before "## Runtime" is the index, which states conditions in
    # its own table. The pointers under test are the ones at the STEP that owns
    # each section — those are what the agent hits mid-phase, and those are the
    # ones that have to carry the condition.
    body = _router().split("## Runtime", 1)[1]
    for name in sorted(_referenced()):
        assert f"sections/{name}" in body, (
            f"{name} is only referenced from the index, not from the step that "
            "owns it — the agent reaching that step would not know to read it"
        )
        idx = body.index(f"sections/{name}")
        stanza = body[max(0, idx - 700) : idx + 400]
        # One literal phrase, not a vocabulary of near-synonyms. A regex that
        # accepts several phrasings drifts into accepting prose that states no
        # condition at all, which is precisely the thing being guarded.
        assert (
            "read only when:" in stanza.lower()
        ), f"the pointer to {name} carries no `Read only when:` condition"


def test_the_router_stays_a_router() -> None:
    """The failure this guards is re-inlining: someone moves a section back for
    convenience and the loaded prompt silently returns to its old size. The
    playbook was ~20.6K tokens before the split and every review paid all of
    it, on both lanes, regardless of scope."""
    size = len(_router()) // CHARS_PER_TOKEN
    assert size < ROUTER_TOKEN_CEILING, (
        f"ORCHESTRATION.md is ~{size} tokens, over the {ROUTER_TOKEN_CEILING} "
        "ceiling. Move a conditional block into sections/ rather than raising it."
    )


def test_the_sections_index_lists_every_section() -> None:
    """The index is what a reader scans to decide what to skip. One missing row
    means a section that is only discoverable by reading to the step that owns
    it — which defeats reading less."""
    index = _router().split("## Runtime")[0]
    for name in sorted(_section_files()):
        assert name in index, (
            f"{name} is missing from the 'Conditional sections' index at the top "
            "of ORCHESTRATION.md"
        )


def test_the_context_budget_binds_whoever_reviews() -> None:
    """§1c's ceiling was written as "per agent call", from when every review
    fanned out. §2a now routes a single-specialist scope to the primary agent,
    and on twelve of fourteen measured @sdk-loop runs nothing was dispatched at
    all — so the only input cap in the playbook governed a code path those
    reviews never took and the primary's context went unbounded.

    That matters twice over, and both are measured: turn latency on the review
    model climbs from ~10s to 75-90s by turn 12 as context accumulates, and
    xai/grok-4.6 doubles its per-token rate above a 200K context.
    """
    router = _router()
    budget = router[router.index("### 1c.") : router.index("## Phase 2")]
    assert (
        "dispatched agent or not" in budget
    ), "§1c's budget must bind the inline reviewer, not only a dispatched agent"
    # The ceiling exists because of latency and price, not because of the
    # model's window. Losing that reasoning is how it gets raised to the window.
    assert "200K" in budget and "75-90s" in budget, (
        "§1c must keep the measured reasons for the ceiling — without them the "
        "next reader raises it to the model's context window"
    )


def test_the_conflicting_rule_stays_in_the_router() -> None:
    """`NEEDS_REBASE` is a terminal verdict on the LOOP lane —
    `VERDICTS_TERMINAL` in sdk_loop_common.py, acted on in sdk_loop_phase.py.
    An early draft of the split filed all of step 8 behind a "mothership only"
    pointer, which would have left the loop lane with no instruction to emit
    that verdict: a conflicted PR would draw ordinary findings, no
    `sdk-review-needs-rebase` label, and the loop would spend rounds on a
    branch no resolve phase can move. Only the sandbox-only BEHIND update
    belongs in a section file.
    """
    router = _router()
    step8 = router[router.index("8. **Branch freshness") :][:2600]
    assert "NEEDS_REBASE" in step8, (
        "the CONFLICTING -> NEEDS_REBASE rule must stay inline: both lanes need "
        "it and the loop lane's terminal-verdict handling depends on it"
    )
    assert "BOTH LANES" in step8
    section = (SECTIONS / "branch-freshness.md").read_text(encoding="utf-8")
    assert "update-branch" in section, "the BEHIND update belongs in the section"
    assert "CONFLICTING" not in section.split("NOTE")[0].split("\n8.")[1], (
        "the CONFLICTING path must not be duplicated into the section file — "
        "two copies of a verdict rule drift"
    )


def test_the_mandatory_read_list_stays_deduplicated() -> None:
    """Round 2 of the trim removed two whole-file reads from Phase 0 step 6:
    review-policy.md (merged into retro-log.md, which CLAUDE.md declares the
    ONLY do-not-flag list) and review.yaml (a paraphrase of the rubric and
    CLAUDE.md). Each was a tool call plus ~750 tokens on every review. The
    regression this guards is the quiet re-add — one line in a read list looks
    harmless, and nothing else would fail.
    """
    router = _router()
    step6 = router[router.index("6. **Read in-repo") : router.index("6b/6c.")]
    assert "- `.mothership/review-policy.md`" not in step6
    assert "- `.mothership/review.yaml`" not in step6
    # The merge target must actually carry the merged content, or the
    # by-design patterns silently stop protecting anything.
    retro = (PLAYBOOK_DIR / "references" / "retro-log.md").read_text(encoding="utf-8")
    assert "By-design patterns" in retro
    assert "ThreadPoolExecutor" in retro, (
        "review-policy.md's patterns are gone from retro-log.md — the "
        "do-not-flag merge has been undone without a replacement"
    )
    # And CLAUDE.md must not resurrect its blanket references/*.md read.
    claude = (PLAYBOOK_DIR / "CLAUDE.md").read_text(encoding="utf-8")
    assert "4. `.mothership/pr-review/references/*.md`" not in claude


def test_the_toolkit_section_is_gated_on_lane_not_just_scope() -> None:
    """Run 33500595871 (a contract-toolkit PR on @sdk-loop) followed 1b-toolkit
    into cloning five private consumer repos. The lane's App token is scoped to
    this repository, git has no other credential helper on the runner, so all
    five died with `could not read Username` — and the phase was later killed
    by the idle watchdog with no verdict. A scope-only gate re-creates that run
    on the next toolkit PR; the pointer must also gate on lane and hand the
    loop lane its fallback.
    """
    router = _router()
    idx = router.index("sections/toolkit-consumer-setup.md")
    stanza = router[max(0, idx - 400) : idx + 1800]
    assert "mothership sandbox" in stanza, (
        "the 1b-toolkit pointer no longer restricts consumer cloning to the "
        "sandbox lane"
    )
    assert "TOOLKIT_ROVER_NOTE" in stanza, (
        "the loop lane lost its fallback — without the Rover note, a toolkit "
        "PR on @sdk-loop either dies cloning or approves without saying the "
        "cross-repo validation never ran"
    )
    section = (SECTIONS / "toolkit-consumer-setup.md").read_text(encoding="utf-8")
    assert "mothership sandbox only" in section

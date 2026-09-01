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
    # BEHIND is now identical on both lanes: report, never update. Nothing
    # about step 8 is lane-conditional any more.
    assert "BOTH LANES: report it, never update it" in step8


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
    assert "requires: clone-private" in stanza, (
        "the 1b-toolkit pointer no longer restricts consumer cloning to lanes "
        "that can actually clone private repos"
    )
    assert "TOOLKIT_ROVER_NOTE" in stanza, (
        "the loop lane lost its fallback — without the Rover note, a toolkit "
        "PR on @sdk-loop either dies cloning or approves without saying the "
        "cross-repo validation never ran"
    )
    section = (SECTIONS / "toolkit-consumer-setup.md").read_text(encoding="utf-8")
    assert "mothership sandbox only" in section


#: Every capability the Runtime table defines. A step may only cite one of
#: these — a typo'd capability resolves to nothing and the step runs everywhere.
CAPABILITIES = {
    "clone-private",
    "reach-proxy",
    "given-scope",
    "given-delta",
    "needs-run-guards",
}

#: Steps that were lane-conditional before the redesign, by a stable anchor in
#: their text. Each must still resolve against the Runtime table. Losing an
#: annotation is silent: the step simply runs on a lane that cannot perform it,
#: which is run 33500595871 exactly.
LANE_CONDITIONAL_STEPS = {
    "4b–5. **Run guards**": "needs-run-guards",
    "6b/6c. **Prior review": "given-delta",
    "11. **Smart agent routing.**": "given-scope",
    "### 1b-toolkit.": "clone-private",
    "### 2b. Wave 2": "reach-proxy",
}


def test_the_runtime_table_defines_every_capability() -> None:
    """The matrix is the single axis every lane-conditional step resolves
    against. A step citing a capability the table does not define resolves to
    nothing, and "resolves to nothing" reads exactly like "no condition"."""
    runtime = _router().split("## Runtime")[1].split("## Time Budgets")[0]
    for cap in CAPABILITIES:
        assert f"`{cap}`" in runtime, f"Runtime table does not define {cap}"
    assert "mothership sandbox" in runtime and "sdk-loop" in runtime


def test_every_lane_conditional_step_still_names_a_capability() -> None:
    """The redesign's real risk. Before it, lane differences were spelled out
    inline at each step; after it they are one word that has to be there. Two
    bugs found today came from a step gated on the wrong axis — CONFLICTING
    gated on lane when it needed neither, toolkit cloning gated on scope when
    the credential decides. This asserts the mapping the matrix replaced."""
    router = _router()
    for anchor, capability in LANE_CONDITIONAL_STEPS.items():
        assert anchor in router, f"step anchor vanished: {anchor!r}"
        idx = router.index(anchor)
        window = router[idx : idx + 1200]
        assert capability in window, (
            f"step {anchor!r} no longer names `{capability}` — it will run on "
            "a lane that cannot perform it"
        )


def test_no_step_cites_an_undefined_capability() -> None:
    """A typo'd tag is worse than a missing one: it looks gated and is not."""
    cited = set(re.findall(r"(?:requires|given): ([a-z-]+)", _router()))
    unknown = cited - CAPABILITIES
    assert not unknown, f"steps cite capabilities the Runtime table lacks: {unknown}"


def test_rationale_lives_outside_the_agent_context() -> None:
    """DESIGN.md is maintainer-facing and must never enter a review's context:
    it is not in Phase 0 step 6's read list and nothing may point the agent at
    it. Rationale in the playbook is paid for on every turn after the read."""
    design = (PLAYBOOK_DIR / "DESIGN.md").read_text(encoding="utf-8")
    assert "The agent never reads this file" in design
    router = _router()
    step6 = router[router.index("6. **Read in-repo") : router.index("6b/6c.")]
    assert "DESIGN.md" not in step6, "DESIGN.md must not be in the read list"
    claude = (PLAYBOOK_DIR / "CLAUDE.md").read_text(encoding="utf-8")
    assert "DESIGN.md" not in claude


def test_the_review_never_updates_a_behind_branch() -> None:
    """`update-branch` writes to someone's PR branch, which the Runtime rule
    forbids on both lanes — the two sentences coexisted in this playbook until
    it was removed. sdk_loop_prep.py holds write scope and refuses for the
    stated reason (a base merge is a change the author did not ask for, and
    the review reads the diff against base regardless). No lane may re-add it,
    so this asserts absence rather than a gate: a gate would just be the same
    exception with a nicer name."""
    corpus = _router() + "\n".join(
        p.read_text(encoding="utf-8") for p in SECTIONS.iterdir()
    )
    # Absence of the CALL, not of the word — step 8 names `update-branch` in
    # order to forbid it, and a test that cannot tell a prohibition from an
    # invocation would force the rule to be written vaguely.
    assert "pulls/$PR_NUMBER/update-branch" not in corpus
    assert "-X PUT -f update_method" not in corpus
    assert "never update it" in corpus
    assert "no exceptions" in _router().split("## Time Budgets")[0]

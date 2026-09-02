"""The specialist briefs, held to the contract the runner actually enforces.

Every failure guarded here is silent at review time. A brief that teaches a
field the comment handler rejects produces a 422 mid-submission; one that
teaches a severity spelling outside the vocabulary fails the round; one that
points at the old corpus buys back the orientation turns the redesign exists to
remove. None of these announce themselves — the review just costs more, or ends
without a verdict.

The briefs are deliberately domain-only. What counts as a finding, how nits
converge, how classes are swept, what the output looks like and how severity is
calibrated all live in `REVIEW.md`, which every specialist already holds. A
brief that restates any of it creates a second copy to keep in sync, and the
inherited corpus is a standing demonstration of where that ends: seven briefs
each carrying their own JSON example, three of which emit fields the poster then
has to strip.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_common import PHASE2_AGENTS  # noqa: E402
from sdk_loop_findings import FINDING_FIELDS, load_severity  # noqa: E402
from sdk_loop_routing import RoutingError, load_routing  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
REPO = pathlib.Path(__file__).resolve().parents[3]
AGENTS = REPO / ".mothership/pr-loop/agents"
ROUTING_DATA = REPO / ".mothership/pr-loop/data/agents.yaml"

#: All seven together. A review reads one to four of them, so this is a ceiling
#: on the whole set rather than a per-file budget — and it is set just above
#: today's size so that growth is a decision somebody makes, not an accretion.
TOTAL_CHAR_CEILING = 14_000


def _briefs() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(AGENTS.glob("*.md"))}


def test_every_dispatchable_agent_has_a_brief() -> None:
    """A registered agent with no brief is dispatched with no domain at all —
    it reviews, plausibly, and covers nothing in particular."""
    missing = sorted(set(PHASE2_AGENTS) - set(_briefs()))
    assert not missing, f"agents registered for dispatch with no brief: {missing}"


def test_no_brief_is_orphaned() -> None:
    """A brief nothing dispatches is dead text that still has to be maintained,
    and its absence from every review is invisible."""
    orphaned = sorted(set(_briefs()) - set(PHASE2_AGENTS))
    assert not orphaned, f"briefs no scope dispatches: {orphaned}"


@pytest.mark.parametrize("name", sorted(_briefs()))
def test_no_brief_points_at_the_old_corpus(name: str) -> None:
    """The redesign's central claim is that the reviewer's first turn already
    holds everything it needs. One pointer into the old corpus reintroduces the
    orientation turns — and prose prohibition demonstrably does not work here,
    which is why the fix is to never mention the files at all."""
    text = _briefs()[name]
    for forbidden in (
        "ORCHESTRATION.md",
        "severity-rubric.yaml",
        "retro-log.md",
        "review-policy.md",
        "review.yaml",
        "references/",
        "pr-review/",
    ):
        assert forbidden not in text, f"{name}.md points at {forbidden}"


@pytest.mark.parametrize("name", sorted(_briefs()))
def test_no_brief_teaches_a_severity_outside_the_vocabulary(name: str) -> None:
    """`severity.yaml` documents three competing spellings in the inherited
    corpus and a fourth (`IMPORTANT`) that belongs to none of them. The runner
    fails the round on an unmapped severity rather than reinterpreting it, so a
    brief teaching the wrong word costs a whole review."""
    text = _briefs()[name]
    valid = set(load_severity().display)
    for stale in ("IMPORTANT", "Critical**", "Minor", "Nit-tier"):
        assert stale not in text, f"{name}.md teaches the stale severity {stale!r}"
    # Any all-caps token that looks like a severity must actually be one.
    for word in ("BLOCKING", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        if word in text:
            assert word in valid, f"{name}.md uses {word}, absent from severity.yaml"


@pytest.mark.parametrize("name", sorted(_briefs()))
def test_no_brief_teaches_a_field_the_handler_rejects(name: str) -> None:
    """Unknown fields 422 the inline-comment request. Three of the inherited
    briefs emit `scope`, `domain_tag` and `guardrail`, which the poster then has
    to strip — a brief should not be able to teach a payload that must be undone."""
    text = _briefs()[name]
    for stripped in ("domain_tag", "escalate_to_jira", "guardrail:"):
        assert (
            stripped not in text
        ), f"{name}.md teaches the rejected field {stripped!r}"
    for line in text.splitlines():
        if line.strip().startswith('"') and '":' in line:
            key = line.strip().split('"')[1]
            assert key in FINDING_FIELDS, f"{name}.md emits unknown field {key!r}"


@pytest.mark.parametrize("name", sorted(_briefs()))
def test_no_brief_restates_the_shared_contract(name: str) -> None:
    """Domain only. The shared judgement contract is in REVIEW.md, which every
    specialist holds; a second copy here is a second thing to keep in sync, and
    it is charged to context on every dispatch."""
    text = _briefs()[name].lower()
    for duplicated in (
        "```json",
        "confidence floor",
        "reviewed_files",
        "ready_to_merge",
        "pack_id",
    ):
        assert duplicated not in text, (
            f"{name}.md restates the shared contract ({duplicated!r}). "
            "That belongs in REVIEW.md, which every specialist already holds."
        )


def test_the_brief_set_stays_small() -> None:
    """Context is the product. The inherited briefs total ~33 KB; this set
    exists to be a fraction of that, and a ceiling is the only thing that stops
    it growing back one reasonable paragraph at a time."""
    total = sum(len(t) for t in _briefs().values())
    assert total <= TOTAL_CHAR_CEILING, (
        f"the brief set is {total} chars, over the {TOTAL_CHAR_CEILING} ceiling. "
        "Cut, or move the material to the runner — do not raise the ceiling casually."
    )


@pytest.mark.parametrize("name", sorted(_briefs()))
def test_every_brief_states_what_earns_a_finding(name: str) -> None:
    """The domain half of the four tests. A brief that lists what to look for
    without saying where the bar sits produces exactly the observation-shaped
    output the resolve loop cannot terminate on."""
    text = _briefs()[name].lower()
    assert (
        "what earns a finding" in text or "how to answer" in text
    ), f"{name}.md never says where its bar sits"


# ---------------------------------------------------------------------------
# Routing — owned by this lane, not mirrored from the other one
# ---------------------------------------------------------------------------


def _emitted_scopes() -> set[str]:
    """Every scope `classify_scope` can return, read from its own source.

    Parsed rather than enumerated by hand: the failure this guards is somebody
    adding a scope to the classifier and not to the routing, and a hand-written
    list here would be updated in the same commit that forgets the routing.
    """
    source = pathlib.Path(SCRIPTS / "sdk_loop_common.py").read_text(encoding="utf-8")
    body = source.split("def classify_scope")[1].split("\ndef ")[0]
    return set(re.findall(r'return "([a-z-]+)"', body))


def test_every_scope_the_classifier_emits_has_a_route() -> None:
    """A scope with no route dispatches nobody and still returns a verdict —
    an approval over a diff nothing read."""
    routing = load_routing(ROUTING_DATA)
    missing = sorted(_emitted_scopes() - set(routing.routes))
    assert not missing, f"scopes the classifier emits with no route: {missing}"


def test_every_routed_agent_has_a_brief() -> None:
    """The other half of the same wire: a route naming an agent with no brief
    dispatches a specialist that has no domain."""
    routing = load_routing(ROUTING_DATA)
    named = {a for r in routing.routes.values() for a in (*r.agents, *r.also)}
    missing = sorted(named - set(_briefs()))
    assert not missing, f"routes dispatch agents with no brief: {missing}"


def test_the_routing_never_reads_the_other_lane(monkeypatch) -> None:
    """The point of moving it, asserted as behaviour rather than as prose.

    Both modules *mention* the old playbook in their docstrings — explaining
    why the routing moved is the reason they exist. What must not happen is a
    read: while `SCOPE_AGENTS` was asserted against a table in
    `pr-review/ORCHESTRATION.md`, that file authored this lane's behaviour, and
    retiring or restructuring it would silently change who reviews what.

    So: make every read of anything under `pr-review/` explode, then load the
    routing. If it survives, the dependency is genuinely gone.
    """
    real_read_text = pathlib.Path.read_text

    def guarded(self, *args, **kwargs):
        if "pr-review" in str(self):
            raise AssertionError(f"the routing read the other lane: {self}")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded)
    routing = load_routing(ROUTING_DATA)
    assert routing.routes, "loaded nothing"
    assert routing.route("full").resolve(
        touches_config=False, touches_conformance=False
    )


def test_docs_only_dispatches_nobody() -> None:
    """Prose findings on prose are the purest effective false positive:
    correct, unactioned, and corrosive to trust in the rest of the review."""
    routing = load_routing(ROUTING_DATA)
    resolved = routing.route("docs-only").resolve(
        touches_config=False, touches_conformance=False
    )
    assert resolved == ()


def test_conditional_specialists_fire_only_on_their_condition() -> None:
    routing = load_routing(ROUTING_DATA)
    full = routing.route("full")
    plain = full.resolve(touches_config=False, touches_conformance=False)
    with_config = full.resolve(touches_config=True, touches_conformance=False)
    assert "reachability" in plain, "an `always` condition did not fire"
    assert "ci-config" not in plain
    assert "ci-config" in with_config
    assert with_config.index("correctness") < with_config.index(
        "ci-config"
    ), "dispatch order is not stable — it shows up in logs"


def test_an_unknown_condition_fails_to_load(tmp_path) -> None:
    """A typo'd condition would never fire, dispatching one specialist fewer
    for the rest of the lane's life without anything going red."""
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "version: 1\n"
        "routes:\n"
        "  full:\n"
        "    agents: [correctness]\n"
        "    also:\n"
        "      reachability: when_the_moon_is_full\n"
        "    why: because\n"
        "depth:\n  - max_changed_lines: null\n    mode: single_pass\n",
        encoding="utf-8",
    )
    with pytest.raises(RoutingError, match="never fire"):
        load_routing(bad)


def test_the_depth_ladder_needs_a_catch_all(tmp_path) -> None:
    """Without one, a large enough diff matches no rule and gets no review."""
    bad = tmp_path / "agents.yaml"
    bad.write_text(
        "version: 1\nroutes: {}\n"
        "depth:\n  - max_changed_lines: 400\n    mode: single_pass\n",
        encoding="utf-8",
    )
    with pytest.raises(RoutingError, match="catch-all|max_changed_lines: null"):
        load_routing(bad)


def test_a_large_diff_is_split_rather_than_reviewed_in_one_pass() -> None:
    """The dimension the inherited table did not have.

    Inspection research puts defect detection at 70-90% for 200-400 changed
    lines and 28% past 1,000. A 4,000-line diff and a 40-line diff in the same
    directory used to draw an identical review; the large one came back just as
    confident and had read far less of it.
    """
    routing = load_routing(ROUTING_DATA)
    assert routing.mode_for(120) == "single_pass"
    assert routing.mode_for(400) == "single_pass"
    assert routing.mode_for(3000) == "per_module"

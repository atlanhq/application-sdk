"""The rules that apply to these paths — and the guarantee that none fall out.

`REVIEW.md` promises the reviewer the rules for its paths are already in its
context. For the first version of the lane that was false, and nothing said so:
108 KB of rules sat in a directory the briefs were forbidden from naming. The
tests here make the promise checkable — every rule in the corpus is reachable
by some specialist for some path, retrieval pulls the right rules for a diff,
and the budget demotes rather than drops.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_common as common  # noqa: E402
import sdk_loop_rules as R  # noqa: E402
from sdk_loop_pack import build_pack, render  # noqa: E402
from sdk_loop_routing import load_routing  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
RULES_DIR = REPO / ".mothership/rules"


@pytest.fixture(scope="module")
def corpus():
    return R.load_corpus()


# ---------------------------------------------------------------------------
# The corpus is whole and reachable
# ---------------------------------------------------------------------------


def test_every_rule_file_on_disk_is_indexed(corpus) -> None:
    """A rule file nobody routes is 12 KB of knowledge no review can reach —
    exactly the state this PR exists to end."""
    on_disk = {p.name for p in RULES_DIR.glob("*-rules.md")}
    indexed = {rf.name for rf in corpus}
    assert (
        on_disk == indexed
    ), f"unindexed: {on_disk - indexed}; missing: {indexed - on_disk}"


def test_every_indexed_file_has_a_specialist_and_a_path(corpus) -> None:
    for rf in corpus:
        assert rf.specialists, f"{rf.name} reaches no specialist"
        assert rf.paths, f"{rf.name} applies to no path"


def test_every_rule_is_reachable_by_some_specialist_for_some_path(corpus) -> None:
    """The invariant. For each rule file, there exists a specialist and a
    changed path for which `select` returns every rule in the file (full or
    by name). A rule that no combination reaches has fallen out of the lane."""
    for rf in corpus:
        specialist = sorted(rf.specialists)[0]
        # Turn the first glob into a concrete path that matches it.
        glob = rf.paths[0]
        probe = glob.replace("**/", "").replace("**", "x").replace("*", "probe")
        if probe.endswith("/") or "." not in probe.split("/")[-1]:
            probe = probe.rstrip("/") + "/probe.py"
        sel = R.select(
            corpus,
            specialist=specialist,
            changed_paths=[probe],
            diff="",
            budget_chars=10**9,
        )
        reached = {r.title for r in sel.full + sel.index if r.file == rf.name}
        expected = {r.title for r in rf.rules}
        assert reached == expected, (
            f"{rf.name}: {sorted(expected - reached)} unreachable for "
            f"{specialist} on {probe}"
        )


def test_the_corpus_parses_into_the_rules_the_headings_promise(corpus) -> None:
    """Every `###` heading is a rule. A parser that merged two rules or dropped
    one would silently shrink the corpus."""
    for rf in corpus:
        text = (RULES_DIR / rf.name).read_text(encoding="utf-8")
        headings = [ln for ln in text.splitlines() if ln.startswith("### ")]
        assert len(rf.rules) == len(
            headings
        ), f"{rf.name}: {len(rf.rules)} vs {len(headings)}"


def test_most_rules_carry_retrieval_identifiers(corpus) -> None:
    """A rule with no identifiers can only ever arrive by name. Inline
    backticks were added as a source precisely because the prose-only rules —
    most of security and architecture — had none from code blocks."""
    total = sum(len(rf.rules) for rf in corpus)
    with_ids = sum(1 for rf in corpus for r in rf.rules if r.identifiers)
    assert with_ids / total >= 0.85, f"only {with_ids}/{total} rules have identifiers"


# ---------------------------------------------------------------------------
# Retrieval — the right rules for this diff
# ---------------------------------------------------------------------------


def test_a_diff_containing_what_a_rule_is_about_gets_it_in_full(corpus) -> None:
    diff = "+    time.sleep(2)\n+    response = requests.get(url)\n"
    sel = R.select(
        corpus,
        specialist="correctness",
        changed_paths=["application_sdk/clients/http.py"],
        diff=diff,
        budget_chars=24_000,
    )
    full = {r.title for r in sel.full}
    assert any("time.sleep" in t for t in full), full
    assert any("Synchronous HTTP" in t for t in full), full


def test_a_prose_only_rule_is_retrieved_by_its_inline_identifiers(corpus) -> None:
    """The contract-discipline rule has no code blocks. `@dataclass` in a diff
    must still pull it in full — that is the whole reason inline backticks are
    an identifier source."""
    diff = "+@dataclass\n+class MyInput(Input):\n"
    sel = R.select(
        corpus,
        specialist="correctness",
        changed_paths=["application_sdk/contracts/x.py"],
        diff=diff,
        budget_chars=24_000,
    )
    assert any("Contract discipline" in r.title for r in sel.full), [
        r.title for r in sel.full
    ]


def test_a_specialist_never_sees_rules_it_does_not_own(corpus) -> None:
    sel = R.select(
        corpus,
        specialist="quality",
        changed_paths=["application_sdk/x.py"],
        diff="+requests.get(url)",
        budget_chars=24_000,
    )
    assert "security-rules.md" not in sel.files
    assert "performance-rules.md" not in sel.files


def test_a_config_only_diff_carries_no_rules(corpus) -> None:
    """Retrieval, not volume: a workflow change has nothing to do with
    performance rules, and carrying them is the old lane again."""
    sel = R.select(
        corpus,
        specialist="ci-config",
        changed_paths=[".github/workflows/x.yml"],
        diff="+on: push",
        budget_chars=24_000,
    )
    assert sel.empty


def test_test_quality_rules_arrive_only_when_tests_change(corpus) -> None:
    src_only = R.select(
        corpus,
        specialist="quality",
        changed_paths=["application_sdk/x.py"],
        diff="",
        budget_chars=24_000,
    )
    with_tests = R.select(
        corpus,
        specialist="quality",
        changed_paths=["tests/unit/test_x.py"],
        diff="",
        budget_chars=24_000,
    )
    assert "test-quality-rules.md" not in src_only.files
    assert "test-quality-rules.md" in with_tests.files


# ---------------------------------------------------------------------------
# The budget demotes, never drops
# ---------------------------------------------------------------------------


def test_the_budget_demotes_to_the_index_and_says_so(corpus) -> None:
    """A rule the diff trips but the budget cannot afford still arrives by
    name, and the pack states that it did. Silent loss is the one outcome this
    module must not have."""
    # Every performance rule's identifiers, so all 17 match.
    perf = next(rf for rf in corpus if rf.name == "performance-rules.md")
    diff = "\n".join(sorted(i for r in perf.rules for i in r.identifiers))
    sel = R.select(
        corpus,
        specialist="correctness",
        changed_paths=["application_sdk/x.py"],
        diff=diff,
        budget_chars=3_000,
    )
    assert sel.demoted > 0
    assert {r.title for r in sel.full + sel.index} >= {r.title for r in perf.rules}
    text = R.render(sel)
    assert "full-text budget was spent" in text


def test_the_most_matched_rules_win_the_budget(corpus) -> None:
    perf = next(rf for rf in corpus if rf.name == "performance-rules.md")
    ranked = sorted(perf.rules, key=lambda r: -len(r.identifiers))
    # A diff that mentions the richest rule's identifiers three times over.
    rich = ranked[0]
    diff = ("\n".join(rich.identifiers) + "\n") * 3 + "\n".join(ranked[-1].identifiers)
    sel = R.select(
        corpus,
        specialist="correctness",
        changed_paths=["application_sdk/x.py"],
        diff=diff,
        budget_chars=len(rich.body) + 10,
    )
    assert sel.full and sel.full[0].title == rich.title


# ---------------------------------------------------------------------------
# What the reviewer sees
# ---------------------------------------------------------------------------


def test_index_lines_carry_the_claim_not_just_the_name(corpus) -> None:
    """A title tells the reviewer a rule exists; the first sentence tells it
    what the rule forbids. For prose-only rules that is most of the value."""
    rule = next(
        r for rf in corpus for r in rf.rules if "Contract discipline" in r.title
    )
    assert " — " in rule.index_line
    assert len(rule.summary) > 40


def test_an_empty_selection_renders_nothing(corpus) -> None:
    assert R.render(R.Selection()) == ""


def test_the_pack_carries_the_rules_section() -> None:
    routing = load_routing()
    diff = (
        "diff --git a/application_sdk/x.py b/application_sdk/x.py\n"
        "--- a/application_sdk/x.py\n+++ b/application_sdk/x.py\n"
        "@@ -0,0 +1 @@\n+    time.sleep(1)\n"
    )
    pack = build_pack(repo=REPO, diff=diff, scope="full", routing=routing)
    sel = R.select(
        R.load_corpus(),
        specialist="correctness",
        changed_paths=[f.path for f in pack.files],
        diff=diff,
        budget_chars=24_000,
    )
    text = render(pack, "correctness", rules_section=R.render(sel))
    assert "## Rules that apply to these paths" in text
    assert "time.sleep" in text


# ---------------------------------------------------------------------------
# Sub-agents get the same context the primary does
# ---------------------------------------------------------------------------


def test_sub_agents_read_the_new_lanes_briefs(monkeypatch) -> None:
    """The first cutover left `_agent_prompt` on `pr-review/agents/`, so every
    dispatched specialist ran the old contract while the primary ran the new
    one. A brief under `pr-loop/agents/` is what a sub-agent must receive."""
    monkeypatch.chdir(REPO)
    text = common._agent_prompt("correctness")
    assert "# Correctness" in text
    assert "references/" not in text and "ORCHESTRATION" not in text


def test_sub_agent_prompts_carry_their_context(monkeypatch) -> None:
    """A sub-agent with a brief and nothing else has a domain and no idea what
    counts as a finding, what the diff is, or which rules apply."""
    monkeypatch.chdir(REPO)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    agents = common.review_subagents(
        "m",
        ("correctness", "quality"),
        context={"correctness": "## Rules that apply\n- PERF-003"},
    )
    assert "PERF-003" in agents["correctness"]["prompt"]
    assert "PERF-003" not in agents["quality"]["prompt"], "context is per specialist"
    assert "# Correctness" in agents["correctness"]["prompt"]


def test_research_discipline_no_longer_points_at_the_old_corpus() -> None:
    for stale in ("retro-log.md", "reference rules you own", "pr-review/references"):
        assert stale not in common.RESEARCH_DISCIPLINE, stale


# ---------------------------------------------------------------------------
# The old lane still finds its rules
# ---------------------------------------------------------------------------


def test_the_sandbox_lanes_briefs_point_at_the_moved_files() -> None:
    """Moving the corpus must not strand the other lane. Every rule path a
    pr-review brief names must exist."""
    for brief in (REPO / ".mothership/pr-review/agents").glob("*.md"):
        text = brief.read_text(encoding="utf-8")
        assert (
            "pr-review/references/" not in text
            or "-rules.md" not in text.split("pr-review/references/")[1].split("\n")[0]
        ), f"{brief.name} still names a rule under references/"
        for rel in re.findall(r"`?(\.mothership/rules/[a-z0-9-]+-rules\.md)`?", text):
            assert (REPO / rel).exists(), f"{brief.name} points at missing {rel}"


# ---------------------------------------------------------------------------
# The toolkit specialist knows which contract a surface reaches
# ---------------------------------------------------------------------------

#: The public vocabulary for consumer-facing toolkit contracts. The old lane's
#: consumer registry maps changed surfaces onto these and runs the checks; the
#: new lane cannot open a consumer, so it must at least name the contract for
#: the human who can.
TOOLKIT_CONTRACTS = (
    "UI rendering compatibility",
    "Manifest substitution compatibility",
    "Workflow execution contract",
    "Generated SDK input contract",
    "Representative app pattern",
)


def test_the_toolkit_brief_maps_surfaces_onto_every_public_contract(
    monkeypatch,
) -> None:
    """A brief that says "say what a consumer sees" without saying which
    consumer contracts exist leaves the reviewer to invent the taxonomy. Each
    contract the registry names must reach the toolkit specialist, tied to a
    concrete surface."""
    monkeypatch.chdir(REPO)
    text = common._agent_prompt("toolkit-review")
    for contract in TOOLKIT_CONTRACTS:
        assert contract in text, f"toolkit brief never names {contract!r}"
    for surface in ("Config.pkl", "_input.py", "task queues", "NativeAppBundle.pkl"):
        assert surface in text, f"toolkit brief does not map {surface!r}"


def test_nothing_the_loop_lane_reads_names_an_internal_repository() -> None:
    """The loop lane's output is posted publicly, and its briefs say so. The
    consumer registry that names repositories and clone locations stays in the
    sandbox lane; nothing under pr-loop/ or rules/ may carry one. The family
    glob `atlanhq/atlan-*-app` is the SDK's own public vocabulary and passes."""
    for path in [*(REPO / ".mothership/pr-loop").rglob("*"), *RULES_DIR.glob("*.md")]:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            named = re.findall(r"atlanhq/([\w.*-]+)", line)
            if any("*" not in n and not n.startswith("application-sdk") for n in named):
                raise AssertionError(
                    f"{path.relative_to(REPO)} names an internal repository: {line.strip()[:100]}"
                )

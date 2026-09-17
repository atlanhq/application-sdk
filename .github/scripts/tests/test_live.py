"""The live path — the test the cutover should have shipped with.

The first cutover flipped the prompt and left the runtime waiting for a comment
the model had just been told not to write. Its tests were green, because they
asserted on the prompt string. Every test here drives the path the way a review
actually runs: a payload that follows `REVIEW.md` (JSON, no comment) goes in,
and what comes out is either a verdict comment or a stated reason there is
none.

The gate tests are the ones that matter most. An empty findings list renders
READY_TO_MERGE, and READY_TO_MERGE casts the CODEOWNER approval — so the gate
is the difference between "the reviewer found nothing" and "the reviewer did
not look".
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_live as live  # noqa: E402
import sdk_loop_redgreen as redgreen  # noqa: E402
import sdk_loop_refute as refute  # noqa: E402
from sdk_loop_by_design import load_by_design  # noqa: E402
from sdk_loop_common import parse_reviewed_head, parse_verdict  # noqa: E402
from sdk_loop_findings import Finding, load_severity  # noqa: E402
from sdk_loop_pack import ChangedFile, Pack, build_pack  # noqa: E402
from sdk_loop_phase import DismissalLedger, review_prompt  # noqa: E402
from sdk_loop_routing import load_routing  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
SHA = "a" * 40
DIFF = """\
diff --git a/application_sdk/app/base.py b/application_sdk/app/base.py
--- a/application_sdk/app/base.py
+++ b/application_sdk/app/base.py
@@ -10,2 +10,3 @@
     x = 1
+    y = 2
"""


@pytest.fixture
def pack():
    return build_pack(repo=REPO, diff=DIFF, scope="full", routing=load_routing())


@pytest.fixture
def sev():
    return load_severity()


def _payload(**over) -> str:
    base = {
        "pack_id": "p",
        "status": "complete",
        "reviewed_files": ["application_sdk/app/base.py"],
        "findings": [],
        "strengths": ["small, focused"],
        "notes": "Fixes the cause.",
    }
    base.update(over)
    return json.dumps(base)


def _finding(**over) -> dict:
    base = {
        "title": "unawaited coroutine",
        "severity": "HIGH",
        "confidence": 0.9,
        "file": "application_sdk/app/base.py",
        "line": 11,
        "evidence": "y = 2",
    }
    base.update(over)
    return base


def _deliver(pack, sev, payload, **over):
    kw = dict(
        payload_text=payload,
        pack=pack,
        sev=sev,
        by_design=None,
        challenge=None,
        challenge_brief="# Refuter",
        challenge_mode=refute.CROSS_FAMILY,
        diff=DIFF,
        redgreen_report=None,
        pr=1,
        pr_title="t",
        reviewed_head=SHA,
        answers_trigger=None,
        model="m",
        run_url="",
    )
    kw.update(over)
    return live.deliver(**kw)


# ---------------------------------------------------------------------------
# The gap, closed
# ---------------------------------------------------------------------------


def test_a_review_that_posts_nothing_still_produces_a_verdict_comment(
    pack, sev
) -> None:
    """The end-to-end claim. The model writes JSON and posts nothing, exactly as
    REVIEW.md instructs; the runner renders a comment every downstream consumer
    can parse. Under the first cutover this path did not exist and every round
    ended OUTCOME_FAILED."""
    result = _deliver(pack, sev, _payload())
    assert result.should_post
    assert parse_verdict(result.body) == "READY_TO_MERGE"
    assert parse_reviewed_head(result.body) == SHA
    assert "<!-- SDK_REVIEW -->" in result.body


def test_the_prompt_names_the_output_path() -> None:
    """REVIEW.md: 'write one JSON object to the path named in your prompt'.
    Nothing named one."""
    prompt = review_prompt(
        1,
        1,
        SHA,
        DismissalLedger(),
        scope="full",
        agents=("correctness",),
        output_path="/w/.sdk-loop/findings.json",
    )
    assert "/w/.sdk-loop/findings.json" in prompt
    assert "Post nothing to the PR yourself" in prompt


# ---------------------------------------------------------------------------
# The completion gate — the load-bearing part
# ---------------------------------------------------------------------------


def test_no_completion_assertion_means_no_comment(pack, sev) -> None:
    """An empty findings list with no `status` is indistinguishable from a
    reviewer that died after creating the file. It must not approve."""
    result = _deliver(pack, sev, _payload(status=None))
    assert not result.should_post
    assert "no completion assertion" in result.failure


def test_complete_must_actually_cover_the_pack(pack, sev) -> None:
    """Claiming complete while listing half the files is the gate failing, not
    the review passing."""
    result = _deliver(pack, sev, _payload(reviewed_files=["some/other.py"]))
    assert not result.should_post
    assert "omits" in result.failure and "application_sdk/app/base.py" in result.failure


def test_empty_reviewed_files_fails_the_gate(pack, sev) -> None:
    result = _deliver(pack, sev, _payload(reviewed_files=[]))
    assert not result.should_post


def _file(path: str, *, deleted: bool = False) -> ChangedFile:
    return ChangedFile(
        path=path,
        added=() if deleted else (1,),
        removed_count=1 if deleted else 0,
        is_deleted=deleted,
    )


def _pack_of(*files: ChangedFile) -> Pack:
    return Pack(scope="config-only", mode="single_pass", agents=(), files=files)


def test_complete_covers_every_non_test_pack_file_not_just_python(sev) -> None:
    """A workflow/Helm pack has no Python source, so the old is_python filter
    left `expected` empty and any dummy reviewed_files list minted
    READY_TO_MERGE. Coverage is every non-deleted, non-test pack file."""
    pack = _pack_of(
        _file(".github/workflows/sdk-loop.yml"),
        _file("helm/chart.yaml"),
        _file("pyproject.toml"),
    )
    result = _deliver(pack, sev, _payload(reviewed_files=["not/the/file.py"]))
    assert not result.should_post
    assert "omits" in result.failure
    covered = _deliver(
        pack,
        sev,
        _payload(
            reviewed_files=[
                ".github/workflows/sdk-loop.yml",
                "helm/chart.yaml",
                "pyproject.toml",
            ]
        ),
    )
    assert covered.should_post
    assert covered.verdict == "READY_TO_MERGE"


def test_a_tests_only_pack_must_cover_the_remaining_paths(sev) -> None:
    """When every non-deleted file is a test, `expected` is empty. Requiring
    the remaining pack paths (the tests themselves) stops any non-empty list
    from counting as a complete review of nothing in particular."""
    pack = _pack_of(_file("tests/test_a.py"), _file(".github/scripts/tests/test_b.py"))
    result = _deliver(pack, sev, _payload(reviewed_files=["unrelated.py"]))
    assert not result.should_post
    assert "omits" in result.failure
    covered = _deliver(
        pack,
        sev,
        _payload(
            reviewed_files=[
                "tests/test_a.py",
                ".github/scripts/tests/test_b.py",
            ]
        ),
    )
    assert covered.should_post


def test_deleted_files_are_not_required_in_reviewed_files(sev) -> None:
    pack = _pack_of(
        _file("application_sdk/app/base.py"), _file("gone.py", deleted=True)
    )
    result = _deliver(pack, sev, _payload())
    assert result.should_post


def test_a_partial_review_posts_but_may_not_approve(pack, sev) -> None:
    """Partial is honest and useful. Partial-with-zero-findings rendering
    READY_TO_MERGE is the exact failure the gate exists to stop."""
    result = _deliver(pack, sev, _payload(status="partial"))
    assert result.should_post
    assert result.verdict == "NEEDS_HUMAN"
    assert "Partial review" in result.body


def test_a_partial_review_with_a_guardrail_is_still_blocked(pack, sev) -> None:
    """The partial floor is a floor. A guardrail's BLOCKED outranks it."""
    guarded = next(p for e in sev.guardrails.values() for p in e["patterns"])
    result = _deliver(
        pack,
        sev,
        _payload(
            status="partial", findings=[_finding(pattern_id=guarded, severity="LOW")]
        ),
    )
    assert result.verdict == "BLOCKED"


def test_a_malformed_payload_is_a_stated_failure_not_an_approval(pack, sev) -> None:
    result = _deliver(pack, sev, "this is not json")
    assert not result.should_post
    assert "payload rejected" in result.failure


# ---------------------------------------------------------------------------
# The stages, actually invoked
# ---------------------------------------------------------------------------


def test_the_by_design_filter_is_applied(pack, sev) -> None:
    """The first cutover shipped the filter and never called it."""
    result = _deliver(
        pack,
        sev,
        _payload(findings=[_finding(pattern_id="T201", title="print in prod")]),
        by_design=load_by_design(),
    )
    assert result.dropped == 1
    assert not result.kept
    assert result.verdict == "READY_TO_MERGE"


def test_an_argued_disagreement_from_the_challenger_drops_the_finding(
    pack, sev
) -> None:
    """The refutation stage, wired. The challenger gets the findings keyed for
    reply and its argued DISAGREE removes one before render."""
    raw = _finding()
    key = None

    def challenger(prompt: str) -> str:
        nonlocal key
        # The prompt carries the exact key arbitrate() will match on.
        line = next(ln for ln in prompt.splitlines() if ln.startswith("### target: `"))
        key = line.split("`")[1]
        return json.dumps(
            {
                "challenges": [
                    {
                        "target": key,
                        "stance": "DISAGREE",
                        "reason": "y is assigned and never awaited because it is not a coroutine; checked the module.",
                    }
                ]
            }
        )

    result = _deliver(pack, sev, _payload(findings=[raw]), challenge=challenger)
    assert key, "the challenger was never called"
    assert not result.kept
    assert result.challenged == refute.CROSS_FAMILY
    assert "different model family" in result.body
    assert "Withdrawn after challenge" in result.body


def test_a_challenger_that_explodes_keeps_every_finding(pack, sev) -> None:
    """Fail open on the challenge — the opposite direction from the gate, on
    purpose. Losing a real defect and manufacturing an approval are different
    failures with different costs."""

    def boom(prompt: str) -> str:
        raise RuntimeError("proxy down")

    result = _deliver(pack, sev, _payload(findings=[_finding()]), challenge=boom)
    assert len(result.kept) == 1
    assert result.challenged == refute.NOT_RUN
    assert "not run" in result.body


def test_no_challenger_is_stated_not_implied(pack, sev) -> None:
    result = _deliver(pack, sev, _payload(findings=[_finding()]), challenge=None)
    assert "not run" in result.body and "as first proposed" in result.body


def test_prose_findings_are_not_sent_to_the_challenger(pack, sev) -> None:
    """LOW/INFO never block, so cross-examining them buys precision nobody pays
    for — and a challenger call is the most expensive step in the round."""
    calls = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    _deliver(pack, sev, _payload(findings=[_finding(severity="LOW")]), challenge=spy)
    assert calls == []


def test_the_redgreen_report_lands_in_the_summary(pack, sev) -> None:
    report = redgreen.Report(
        outcomes=[
            redgreen.Outcome(
                redgreen.TestRef("tests/t.py", "test_a"), redgreen.CAPTURED
            ),
            redgreen.Outcome(
                redgreen.TestRef("tests/t.py", "test_b"), redgreen.NOT_CAPTURED
            ),
        ]
    )
    result = _deliver(pack, sev, _payload(), redgreen_report=report)
    assert "Red-green:" in result.body and "1/2" in result.body


# ---------------------------------------------------------------------------
# The pieces around the path
# ---------------------------------------------------------------------------


def test_refute_prompt_keys_findings_the_way_arbitrate_matches(sev) -> None:
    f = Finding(title="t", severity="HIGH", confidence=0.9, file="a.py", line=3)
    prompt = live.refute_prompt("# brief", [f], DIFF)
    assert f"### target: `{refute.finding_key(f)}`" in prompt
    assert "```diff" in prompt


def test_redgreen_job_reports_a_crash_instead_of_raising(tmp_path) -> None:
    """Advisory means advisory. A red-green failure must never take the review
    down with it."""

    def runner(args, cwd):
        raise OSError("git is missing")

    job = live.RedGreenJob(
        repo=tmp_path, base_ref="abc", files=[], workdir=tmp_path / "wt", runner=runner
    )
    report = job.join(timeout_s=5)
    assert report.skipped_reason  # either "no test functions" or the raised error


def test_post_comment_sends_the_body_on_stdin() -> None:
    """A rendered summary can exceed the argument-length limit, and a truncated
    body loses the marker block every consumer parses."""
    seen = {}

    class R:
        stdout = json.dumps({"html_url": "https://x/1"})

    def sh(args, **kw):
        seen["args"] = args
        seen["input"] = kw.get("input")
        return R()

    url = live.post_comment("o/r", 7, "BODY" * 5000, sh)
    assert "--input" in seen["args"] and "-" in seen["args"]
    assert json.loads(seen["input"])["body"].startswith("BODY")
    assert not any(a.startswith("body=") for a in seen["args"])
    assert url == "https://x/1"


def test_post_comment_returns_empty_when_gh_writes_nothing() -> None:
    class R:
        stdout = ""

    assert live.post_comment("o/r", 7, "BODY", lambda *a, **k: R()) == ""


def test_suppressed_findings_are_rendered_where_a_human_can_see_them(pack, sev) -> None:
    """The clause that justifies machine suppression. `Normalised.dropped` had a
    good audit string and no consumer, so "over-suppression is discoverable
    instead of silent" was true of a data structure and false of anything the
    author saw. Now it is in the comment — collapsed, but there."""
    result = _deliver(
        pack,
        sev,
        _payload(
            findings=[_finding(pattern_id="T201", title="print in prod", line=11)]
        ),
        by_design=load_by_design(),
    )
    assert result.dropped == 1
    assert "Suppressed before review (1)" in result.body
    assert "print in prod" in result.body
    assert (
        "ci-logging-hygiene" in result.body
    ), "the entry that caused the drop is named"
    assert (
        "<details>" in result.body
    ), "collapsed — there to be checked, not read every time"


def test_nothing_suppressed_renders_no_section(pack, sev) -> None:
    result = _deliver(
        pack, sev, _payload(findings=[_finding()]), by_design=load_by_design()
    )
    assert "Suppressed before review" not in result.body

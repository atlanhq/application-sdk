"""Tests for the duplicate-verdict collapse (FND-636).

The shape being defended against is PR #3276: one review run, five identical
`<!-- SDK_REVIEW -->` summaries inside 76 seconds, because a provider-level
retry replayed the assistant turn that posts the comment.

The shape that must NOT be collapsed is someone else's summary landing in the
same window — a zombie sandbox that outlived its cancelled job, or a concurrent
human-triggered run. Attribution is by run URL for that reason; the attribution
decision itself is tested in test_sdk_review_summaries.py, and what this module
does with the answer is tested here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sdk_review_dedupe_verdicts", SCRIPTS / "sdk_review_dedupe_verdicts.py"
)
dedupe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dedupe)

SINCE = "2026-08-19T18:09:30.123Z"
RUN_URL = "https://github.com/atlanhq/application-sdk/actions/runs/32286845311"
OTHER_RUN_URL = "https://github.com/atlanhq/application-sdk/actions/runs/32286821875"
HEAD = "4237280fd9dd1eb52c8b3a0eb44e4f4a3c2d1b0a"
OTHER_HEAD = "51c160b06a2a350289c7d779f4ab887503f98685"


def verdict(
    comment_id: int,
    created_at: str,
    run_url: str | None = RUN_URL,
    head: str | None = HEAD,
    node: str | None = None,
) -> dict:
    """A summary comment as ORCHESTRATION.md §3e specifies it."""
    lines = ["<!-- SDK_REVIEW -->", "<!-- VERDICT: READY_TO_MERGE -->"]
    if head is not None:
        lines.append(f"<!-- REVIEWED_HEAD: {head} -->")
    lines += ["## SDK Review (mothership)", "", "---", "**CI:** all passing"]
    if run_url is not None:
        lines.append(f"**Run:** [view workflow logs + cost]({run_url})")
    return {
        "id": comment_id,
        "node_id": node if node is not None else f"IC_kw{comment_id}",
        "created_at": created_at,
        "body": "\n".join(lines),
    }


def chatter(comment_id: int, created_at: str) -> dict:
    return {
        "id": comment_id,
        "node_id": f"IC_kw{comment_id}",
        "created_at": created_at,
        "body": "@sdk-review",
    }


def fake_runner(pages, returncode: int = 0):
    """Answers the comments read; records every `gh` invocation."""
    calls: list[list[str]] = []

    def _run(args, *_a, **_kw):
        calls.append(args)
        if args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return subprocess.CompletedProcess(args, returncode, json.dumps(pages), "")

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def graphql_calls(runner) -> list[list[str]]:
    return [c for c in runner.calls if c[:3] == ["gh", "api", "graphql"]]


def outputs(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
    )


def _env(monkeypatch: pytest.MonkeyPatch, out: Path, **extra: str) -> None:
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "3276")
    monkeypatch.setenv("SINCE", SINCE)
    monkeypatch.setenv("GHA_RUN_URL", RUN_URL)
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("DEDUPE_OUTPUT", str(out))
    monkeypatch.delenv("DRY_RUN", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


# --- main(): the #3276 burst --------------------------------------------


def test_keeps_the_newest_and_minimizes_the_rest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [
        verdict(1, "2026-08-19T18:09:39Z"),
        verdict(2, "2026-08-19T18:14:35Z"),
        verdict(3, "2026-08-19T18:14:43Z"),
        verdict(4, "2026-08-19T18:14:50Z"),
        verdict(5, "2026-08-19T18:14:55Z"),
    ]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0

    minimized = graphql_calls(runner)
    assert len(minimized) == 4
    hidden = {arg for call in minimized for arg in call if arg.startswith("id=")}
    assert hidden == {"id=IC_kw1", "id=IC_kw2", "id=IC_kw3", "id=IC_kw4"}
    assert outputs(out) == {
        "verdict_posted": "1",
        "verdict_count": "5",
        "minimized_count": "4",
        "attribution": "run-url",
    }


def test_our_verdict_is_found_however_late_it_lands(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """No window can drop our own verdict — the run URL is what finds it.

    This is the case a lenient window was proposed to rescue: a summary whose
    `created_at` sits outside the starter bound. Attribution never consults the
    bound for our own summary, so the window stays strict and this still counts.
    """
    out = tmp_path / "out"
    _env(monkeypatch, out, SINCE="2026-08-19T18:09:39.900Z")
    runner = fake_runner([[verdict(1, "2026-08-19T18:09:39Z")]])

    assert dedupe.main(runner) == 0
    assert outputs(out)["verdict_posted"] == "1"
    assert outputs(out)["attribution"] == "run-url"


def test_the_normal_single_summary_is_left_alone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)
    runner = fake_runner([[verdict(1, "2026-08-19T18:14:35Z")]])

    assert dedupe.main(runner) == 0

    assert graphql_calls(runner) == []
    assert outputs(out) == {
        "verdict_posted": "1",
        "verdict_count": "1",
        "minimized_count": "0",
        "attribution": "run-url",
    }


# --- main(): summaries that are not ours --------------------------------


def test_a_zombie_sandboxs_summary_in_our_window_is_never_minimized(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Our own verdict must not be hidden as the DUPLICATE of someone else's."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [
        verdict(1, "2026-08-19T18:14:35Z"),  # ours
        verdict(2, "2026-08-19T18:16:00Z", run_url=OTHER_RUN_URL),  # the zombie's
    ]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0

    assert graphql_calls(runner) == []
    assert outputs(out) == {
        "verdict_posted": "1",
        "verdict_count": "1",
        "minimized_count": "0",
        "attribution": "run-url",
    }


def test_another_runs_summary_alone_does_not_claim_a_delivered_review(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Soft-success must not fire when our sandbox posted nothing.

    The summary is for our head and inside our window — the only thing that
    says it is not ours is the run URL in its footer. Before that was the
    ownership key this reported `verdict_posted=1`, so the dispatch step
    soft-succeeded on a review this run never delivered.
    """
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=OTHER_RUN_URL)]

    assert dedupe.main(fake_runner([comments])) == 0
    assert outputs(out) == {
        "verdict_posted": "0",
        "verdict_count": "0",
        "minimized_count": "0",
        "attribution": "none",
    }


def test_another_head_in_our_window_is_not_a_delivered_review(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [
        verdict(1, "2026-08-19T18:14:35Z", run_url=OTHER_RUN_URL, head=OTHER_HEAD)
    ]

    assert dedupe.main(fake_runner([comments])) == 0
    assert outputs(out) == {
        "verdict_posted": "0",
        "verdict_count": "0",
        "minimized_count": "0",
        "attribution": "none",
    }


def test_a_previous_runs_duplicate_pair_is_not_touched(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Only what this run posted is ours to tidy; older reviews stay visible."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    runner = fake_runner(
        [
            [
                verdict(1, "2026-08-19T17:33:00Z", run_url=OTHER_RUN_URL),
                verdict(2, "2026-08-19T17:37:00Z", run_url=OTHER_RUN_URL),
            ]
        ]
    )

    assert dedupe.main(runner) == 0

    assert graphql_calls(runner) == []
    assert outputs(out)["verdict_posted"] == "0"


def test_a_footerless_duplicate_pair_is_counted_but_not_hidden(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Without the run URL we cannot tell our duplicate from another review."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [
        verdict(1, "2026-08-19T18:14:35Z", run_url=None),
        verdict(2, "2026-08-19T18:14:43Z", run_url=None),
    ]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0

    assert graphql_calls(runner) == []
    assert outputs(out) == {
        "verdict_posted": "1",
        "verdict_count": "2",
        "minimized_count": "0",
        "attribution": "window-fallback",
    }


def test_no_verdict_reports_zero_so_the_dispatch_step_still_hard_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)

    assert dedupe.main(fake_runner([[chatter(1, "2026-08-19T18:10:00Z")]])) == 0
    assert outputs(out)["verdict_posted"] == "0"
    assert outputs(out)["attribution"] == "none"


def test_nothing_to_attribute_by_touches_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out, SINCE="", GHA_RUN_URL="")

    def _explode(*_a, **_kw):
        raise AssertionError("queried GitHub with nothing to attribute by")

    assert dedupe.main(_explode) == 0
    assert outputs(out) == {
        "verdict_posted": "0",
        "verdict_count": "0",
        "minimized_count": "0",
        "attribution": "none",
    }


def test_a_run_url_alone_is_enough_to_attribute(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """An empty starter timestamp no longer disables the collapse."""
    out = tmp_path / "out"
    _env(monkeypatch, out, SINCE="")
    comments = [verdict(1, "2026-08-19T18:14:35Z"), verdict(2, "2026-08-19T18:14:43Z")]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0
    assert len(graphql_calls(runner)) == 1
    assert outputs(out)["attribution"] == "run-url"


def test_pages_are_flattened_before_counting(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """--paginate --slurp returns one array per page; duplicates can straddle."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    runner = fake_runner(
        [[verdict(1, "2026-08-19T18:14:35Z")], [verdict(2, "2026-08-19T18:14:43Z")]]
    )

    assert dedupe.main(runner) == 0
    assert outputs(out)["verdict_count"] == "2"
    assert len(graphql_calls(runner)) == 1


# --- failure handling ----------------------------------------------------


def test_a_failed_comments_read_reports_no_verdict_without_raising(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)

    assert dedupe.main(fake_runner([], returncode=1)) == 0
    assert outputs(out)["verdict_posted"] == "0"


def test_a_refused_minimize_is_reported_not_raised(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """GitHub refusing to hide a comment must not red-check a landed review."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [verdict(1, "2026-08-19T18:09:39Z"), verdict(2, "2026-08-19T18:14:35Z")]

    def _run(args, *_a, **_kw):
        if args[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(args, 1, "", "403 Forbidden")
        return subprocess.CompletedProcess(args, 0, json.dumps([comments]), "")

    assert dedupe.main(_run) == 0
    assert outputs(out) == {
        "verdict_posted": "1",
        "verdict_count": "2",
        "minimized_count": "0",
        "attribution": "run-url",
    }


def test_a_duplicate_with_no_node_id_is_skipped_not_fatal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)
    comments = [
        verdict(1, "2026-08-19T18:09:39Z", node=""),
        verdict(2, "2026-08-19T18:14:35Z"),
    ]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0
    assert graphql_calls(runner) == []
    assert outputs(out)["minimized_count"] == "0"


def test_dry_run_counts_without_calling_graphql(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out, DRY_RUN="true")
    comments = [verdict(1, "2026-08-19T18:09:39Z"), verdict(2, "2026-08-19T18:14:35Z")]
    runner = fake_runner([comments])

    assert dedupe.main(runner) == 0
    assert graphql_calls(runner) == []
    assert outputs(out)["minimized_count"] == "1"


# --- the mutation itself -------------------------------------------------


def test_the_mutation_hides_the_comment_as_a_duplicate():
    assert "minimizeComment" in dedupe.MINIMIZE_MUTATION
    assert "classifier: DUPLICATE" in dedupe.MINIMIZE_MUTATION


def test_minimize_passes_the_node_id_as_a_graphql_variable():
    runner = fake_runner([])
    assert dedupe.minimize("IC_kwABC", runner) is True
    (call,) = graphql_calls(runner)
    assert f"query={dedupe.MINIMIZE_MUTATION}" in call
    assert "id=IC_kwABC" in call

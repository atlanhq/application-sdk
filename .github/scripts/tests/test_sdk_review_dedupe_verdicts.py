"""Tests for the duplicate-verdict collapse (FND-636).

The shape being defended against is PR #3276: one review run, five identical
`<!-- SDK_REVIEW -->` summaries inside 76 seconds, because a provider-level
retry replayed the assistant turn that posts the comment.
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


def verdict(comment_id: int, created_at: str, node: str | None = None) -> dict:
    return {
        "id": comment_id,
        "node_id": node if node is not None else f"IC_kw{comment_id}",
        "created_at": created_at,
        "body": (
            "<!-- SDK_REVIEW -->\n"
            "<!-- VERDICT: READY_TO_MERGE -->\n"
            "<!-- REVIEWED_HEAD: 4237280fd9dd1eb52c8b3a0eb44e4f4a3c2d1b0a -->\n"
            "## SDK Review (mothership)\n"
        ),
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
    monkeypatch.setenv("DEDUPE_OUTPUT", str(out))
    monkeypatch.delenv("DRY_RUN", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


# --- created_at_or_after() -----------------------------------------------


def test_bound_compares_instants_not_strings():
    """`…:39Z` is BEFORE `…:39.123Z` even though "Z" > "." lexicographically."""
    assert not dedupe.created_at_or_after(
        "2026-08-19T18:09:39Z", "2026-08-19T18:09:39.123Z"
    )
    assert "2026-08-19T18:09:39Z" >= "2026-08-19T18:09:39.123Z"  # the naive form


def test_bound_is_inclusive():
    assert dedupe.created_at_or_after(SINCE, SINCE)


def test_bound_accepts_a_later_instant():
    assert dedupe.created_at_or_after("2026-08-19T18:14:55Z", SINCE)


def test_unparseable_timestamps_fall_back_to_string_order():
    assert dedupe.created_at_or_after("zzz", "aaa")


# --- this_runs_summaries() ----------------------------------------------


def test_only_this_runs_summaries_count():
    """A summary from a previous head must not be attributed to this run."""
    comments = [
        verdict(1, "2026-08-19T17:33:00Z"),  # previous run
        chatter(2, "2026-08-19T18:10:00Z"),
        verdict(3, "2026-08-19T18:14:35Z"),
    ]
    assert [c["id"] for c in dedupe.this_runs_summaries(comments, SINCE)] == [3]


def test_same_second_duplicates_are_ordered_by_comment_id():
    """created_at is second-precision; ids break the tie in posting order."""
    comments = [
        verdict(30, "2026-08-19T18:14:50Z"),
        verdict(10, "2026-08-19T18:14:50Z"),
        verdict(20, "2026-08-19T18:14:50Z"),
    ]
    assert [c["id"] for c in dedupe.this_runs_summaries(comments, SINCE)] == [
        10,
        20,
        30,
    ]


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
    }


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
    }


def test_a_previous_runs_duplicate_pair_is_not_touched(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Only this run's window is ours to tidy; older reviews stay visible."""
    out = tmp_path / "out"
    _env(monkeypatch, out)
    runner = fake_runner(
        [[verdict(1, "2026-08-19T17:33:00Z"), verdict(2, "2026-08-19T17:37:00Z")]]
    )

    assert dedupe.main(runner) == 0

    assert graphql_calls(runner) == []
    assert outputs(out)["verdict_posted"] == "0"


def test_no_verdict_reports_zero_so_the_dispatch_step_still_hard_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "out"
    _env(monkeypatch, out)

    assert dedupe.main(fake_runner([[chatter(1, "2026-08-19T18:10:00Z")]])) == 0
    assert outputs(out)["verdict_posted"] == "0"


def test_an_empty_bound_reports_no_verdict_and_touches_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Without the starter timestamp we cannot tell our summary from anyone's."""
    out = tmp_path / "out"
    _env(monkeypatch, out, SINCE="")

    def _explode(*_a, **_kw):
        raise AssertionError("queried GitHub with no lower bound")

    assert dedupe.main(_explode) == 0
    assert outputs(out) == {
        "verdict_posted": "0",
        "verdict_count": "0",
        "minimized_count": "0",
    }


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

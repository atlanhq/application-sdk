"""Tests for re-arming auto-merge on green Renovate PRs that lost it (FND-2940).

GitHub is replaced by a recording fake at the ``post`` seam, so every test sees
exactly which queries and mutations the script would send. The mutation is the
only write this script makes, so most tests assert on whether it was sent.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import renovate_rearm_automerge as rearm  # noqa: E402

try:  # Vocabulary pin: the literal must match the classifier's enum value.
    from conformance.renovate.models import BlockingReason as _BlockingReason
except ImportError:  # pragma: no cover - depends on the runner's environment
    _BlockingReason = None  # type: ignore[assignment]


def _write_repo_report(out_dir, slug: str, prs: list[dict]) -> None:
    """Write one repos/<slug>.json in the shape the renovate-scan CLI emits."""
    repos = out_dir / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    (repos / f"{slug}.json").write_text(
        json.dumps({"repo": f"atlanhq/{slug}", "openPRs": prs}),
        encoding="utf-8",
    )


def _pr(number: int, blocking: str = rearm.NOT_ARMED) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/atlanhq/example/pull/{number}",
        "blockingReason": blocking,
    }


def _state(
    *,
    state: str = "OPEN",
    draft: bool = False,
    armed: bool = False,
    ejections: int = 1,
    auto_merge_allowed: bool = True,
    squash: bool = True,
    merge: bool = True,
    rebase: bool = True,
) -> dict:
    return {
        "data": {
            "repository": {
                "autoMergeAllowed": auto_merge_allowed,
                "squashMergeAllowed": squash,
                "mergeCommitAllowed": merge,
                "rebaseMergeAllowed": rebase,
                "pullRequest": {
                    "id": "PR_node",
                    "state": state,
                    "isDraft": draft,
                    "autoMergeRequest": {"enabledAt": "2026-09-28T00:00:00Z"}
                    if armed
                    else None,
                    "ejections": {"filteredCount": ejections},
                },
            }
        }
    }


class FakeGitHub:
    """Answers the state query with ``state`` and records every call."""

    def __init__(
        self,
        state: Optional[dict] = None,
        enable_response: Optional[dict] = None,
        raise_on: str = "",
    ) -> None:
        self.state = state if state is not None else _state()
        self.enable_response = (
            enable_response
            if enable_response is not None
            else {"data": {"enablePullRequestAutoMerge": {"pullRequest": {}}}}
        )
        self.raise_on = raise_on
        self.calls: list[dict] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append(payload)
        is_mutation = "enablePullRequestAutoMerge" in payload["query"]
        kind = "enable" if is_mutation else "state"
        if self.raise_on == kind:
            raise RuntimeError(f"GraphQL request failed: 502 Bad Gateway ({kind})")
        return self.enable_response if is_mutation else self.state

    @property
    def mutations(self) -> list[dict]:
        return [c for c in self.calls if "enablePullRequestAutoMerge" in c["query"]]


_CANDIDATE = rearm.Candidate(repo="atlanhq/app-one", number=7, url="u")


def test_arms_an_eligible_pr_with_squash() -> None:
    gh = FakeGitHub()
    result = rearm.rearm("t", _CANDIDATE, post=gh)
    assert result.outcome is rearm.Outcome.ARMED
    assert gh.mutations == [
        {
            "query": rearm._ENABLE_MUTATION,
            "variables": {"pullRequestId": "PR_node", "mergeMethod": "SQUASH"},
        }
    ]
    # The state query is scoped to the candidate's own repo and number.
    assert gh.calls[0]["variables"] == {
        "owner": "atlanhq",
        "name": "app-one",
        "number": 7,
    }


@pytest.mark.parametrize(
    ("squash", "merge", "rebase", "expected"),
    [
        (True, True, True, "SQUASH"),
        (False, True, True, "MERGE"),
        (False, False, True, "REBASE"),
    ],
)
def test_merge_method_follows_renovates_order(squash, merge, rebase, expected) -> None:
    gh = FakeGitHub(state=_state(squash=squash, merge=merge, rebase=rebase))
    result = rearm.rearm("t", _CANDIDATE, post=gh)
    assert result.outcome is rearm.Outcome.ARMED
    assert gh.mutations[0]["variables"]["mergeMethod"] == expected


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (_state(state="MERGED"), rearm.Outcome.NOT_OPEN),
        (_state(state="CLOSED"), rearm.Outcome.NOT_OPEN),
        (_state(draft=True), rearm.Outcome.DRAFT),
        (_state(armed=True), rearm.Outcome.ALREADY_ARMED),
        (_state(auto_merge_allowed=False), rearm.Outcome.REPO_DISALLOWS),
        (
            _state(squash=False, merge=False, rebase=False),
            rearm.Outcome.ERROR,
        ),
    ],
)
def test_skips_without_writing_when_no_longer_eligible(state, outcome) -> None:
    gh = FakeGitHub(state=state)
    assert rearm.rearm("t", _CANDIDATE, post=gh).outcome is outcome
    assert gh.mutations == []


def test_ejection_cap_boundary() -> None:
    below = FakeGitHub(state=_state(ejections=rearm.MAX_EJECTIONS - 1))
    assert rearm.rearm("t", _CANDIDATE, post=below).outcome is rearm.Outcome.ARMED

    at = FakeGitHub(state=_state(ejections=rearm.MAX_EJECTIONS))
    result = rearm.rearm("t", _CANDIDATE, post=at)
    assert result.outcome is rearm.Outcome.TOO_MANY_EJECTIONS
    assert at.mutations == []


def test_never_ejected_pr_is_armed() -> None:
    """Arming failed at creation (e.g. a 403 on a spent budget): 0 ejections."""
    gh = FakeGitHub(state=_state(ejections=0))
    assert rearm.rearm("t", _CANDIDATE, post=gh).outcome is rearm.Outcome.ARMED


def test_dry_run_reads_but_never_writes() -> None:
    gh = FakeGitHub()
    result = rearm.rearm("t", _CANDIDATE, dry_run=True, post=gh)
    assert result.outcome is rearm.Outcome.WOULD_ARM
    assert result.detail == "SQUASH"
    assert len(gh.calls) == 1
    assert gh.mutations == []


@pytest.mark.parametrize("raise_on", ["state", "enable"])
def test_transport_failure_is_an_error_result_not_an_exception(raise_on) -> None:
    gh = FakeGitHub(raise_on=raise_on)
    result = rearm.rearm("t", _CANDIDATE, post=gh)
    assert result.outcome is rearm.Outcome.ERROR
    assert "502" in result.detail


def test_graphql_errors_on_enable_are_reported() -> None:
    gh = FakeGitHub(
        enable_response={
            "data": None,
            "errors": [{"message": "Resource not accessible by integration"}],
        }
    )
    result = rearm.rearm("t", _CANDIDATE, post=gh)
    assert result.outcome is rearm.Outcome.ERROR
    assert "Resource not accessible by integration" in result.detail


def test_graphql_errors_on_state_query_do_not_write() -> None:
    gh = FakeGitHub(state={"data": None, "errors": [{"message": "Not found"}]})
    result = rearm.rearm("t", _CANDIDATE, post=gh)
    assert result.outcome is rearm.Outcome.ERROR
    assert gh.mutations == []


def test_candidates_selects_only_not_armed(tmp_path) -> None:
    _write_repo_report(
        tmp_path,
        "app-one",
        [_pr(1), _pr(2, "checks_failing"), _pr(3, "automerge_stale")],
    )
    _write_repo_report(tmp_path, "app-two", [_pr(4)])
    found = rearm.candidates(str(tmp_path))
    assert [(c.repo, c.number) for c in found] == [
        ("atlanhq/app-one", 1),
        ("atlanhq/app-two", 4),
    ]


def test_candidates_survives_bad_reports(tmp_path, capsys) -> None:
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "broken.json").write_text("{not json", encoding="utf-8")
    (repos / "norepo.json").write_text(json.dumps({"openPRs": [_pr(1)]}))
    _write_repo_report(tmp_path, "app-one", [_pr(9)])
    found = rearm.candidates(str(tmp_path))
    assert [c.number for c in found] == [9]
    err = capsys.readouterr().err
    assert "unreadable repo report: broken.json" in err
    assert "repo report without a repo: norepo.json" in err


def test_candidates_missing_dir_is_empty(tmp_path) -> None:
    assert rearm.candidates(str(tmp_path / "absent")) == []


def test_main_caps_per_run_and_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    _write_repo_report(tmp_path, "app-one", [_pr(n) for n in range(1, 6)])
    gh = FakeGitHub()
    code = rearm.main(["--out-dir", str(tmp_path), "--max-rearms", "2"], post=gh)
    assert code == 0
    assert len(gh.mutations) == 2
    out = capsys.readouterr().out
    assert "not-armed PRs: 5, handling 2" in out
    assert "3 not-armed PR(s) left for the next run" in out


def test_main_warns_but_passes_on_errors_and_ejection_cap(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    _write_repo_report(tmp_path, "app-one", [_pr(1)])
    gh = FakeGitHub(state=_state(ejections=rearm.MAX_EJECTIONS))
    assert rearm.main(["--out-dir", str(tmp_path)], post=gh) == 0
    out = capsys.readouterr().out
    assert "::warning::atlanhq/app-one#1 was ejected from the merge queue 3 times" in (
        out
    )

    gh = FakeGitHub(raise_on="enable")
    assert rearm.main(["--out-dir", str(tmp_path)], post=gh) == 0
    assert "::warning::could not re-arm atlanhq/app-one#1" in capsys.readouterr().out


def test_main_requires_a_token_unless_dry_run(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _write_repo_report(tmp_path, "app-one", [_pr(1)])
    assert rearm.main(["--out-dir", str(tmp_path)], post=FakeGitHub()) == 2
    gh = FakeGitHub()
    assert rearm.main(["--out-dir", str(tmp_path), "--dry-run"], post=gh) == 0
    assert gh.mutations == []


@pytest.mark.skipif(_BlockingReason is None, reason="conformance not importable")
def test_not_armed_literal_matches_the_classifier() -> None:
    assert rearm.NOT_ARMED == _BlockingReason.AUTOMERGE_NOT_ARMED.value

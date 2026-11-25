"""Tests for Release Gate (.github/scripts/release_gate.py) and its shim.

Two duties, both on the head commit (see the module docstring):

* a release PR merges only on a `success` `gate-check` status that
  github-actions[bot] posted, and never while pinned to an unreleased SDK
  (FND-3839, FND-3947) — unchanged since the script was vendored;
* every pull request the connector reviewer would review needs the reviewer
  App's `connector-reviewer` status to be `success` (FND-5321). One it would
  not review (its own classifier) passes without one, until it has been
  reviewed once.

The shim tests pin what closes FND-5321's gap: the gate runs from an
application-sdk checkout, never from the pull request.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import connector_reviewer.github as reviewer_github
import gate_check_verdict
import pr_test_command
import pytest
import release_gate as rg
import yaml
from conformance.bootstrap.render import (
    MANAGED_ACTION_FILES,
    RETIRED_ACTION_FILES,
    RETIRED_FILES,
    render,
)
from connector_reviewer.exempt import DOCS, VERSION_BUMP, Exemption
from connector_reviewer.findings import Finding, PRState
from connector_reviewer.github import STATUS_CONTEXT, bot_login
from connector_reviewer.review import SUMMARY_MARKER
from pr_test_command import GitHub, GitHubError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / ".github" / "scripts"
HEAD = "a" * 40
OTHER = "b" * 40
BUMP = "bump-version-main"
ACTIONS_BOT = "github-actions[bot]"
APP = "atlan-app-fleet[bot]"
# `_gather` swaps the module attribute for a stub factory; this stays real.
_REAL_REVIEWER_GITHUB = reviewer_github.GitHub


def _row(
    context: str, state: str, creator: str, description: str = ""
) -> dict[str, Any]:
    return {
        "context": context,
        "state": state,
        "creator": {"login": creator},
        "description": description,
        "target_url": "https://example.invalid/summary",
    }


def _finding(
    fid: str,
    severity: str = "medium",
    *,
    title: str = "",
    rule_id: str = "FP01",
    suggestion: str = "",
    status: str = "open",
) -> Finding:
    f = Finding(
        path="app/x.py",
        line=12,
        severity=severity,
        category="bug",
        title=title or f"title of {fid}",
        body="body",
        evidence="code",
        rule_id=rule_id,
        suggestion=suggestion,
    )
    f.id = fid
    f.status = status
    return f


def _facts(
    rows: list[dict[str, Any]],
    state: PRState | None = None,
    exemption: Exemption | None = None,
) -> rg.ReviewFacts:
    return rg.review_facts(
        rows=rows,
        reviewer_login=APP,
        load_state=lambda: state,
        classify=lambda: exemption,
    )


# ---- release PRs (behaviour carried over unchanged) ------------------------


def test_a_non_release_pr_has_no_release_duty() -> None:
    assert rg.decide_release("feat/x", ["size/S"], "", []) is None


def test_bump_branch_is_a_release_pr_without_the_label() -> None:
    """The `release` label is added after the PR opens, so the branch alone
    must make it a release PR, or every bump PR's first run passes."""
    assert rg.decide_release(BUMP, [], "", []) == (False, rg.MISSING_HINT)


def test_release_label_alone_is_a_release_pr() -> None:
    assert rg.decide_release("hotfix", ["release"], "", []) == (False, rg.MISSING_HINT)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("success", (True, rg.PASSED)),
        ("pending", (False, rg.PENDING_HINT)),
        ("failure", (False, rg.FAILED_HINT)),
        ("error", (False, rg.FAILED_HINT)),
        ("", (False, rg.MISSING_HINT)),
    ],
)
def test_gate_check_states(state: str, expected: tuple[bool, str]) -> None:
    assert rg.decide_release(BUMP, ["release"], state, []) == expected


@pytest.mark.parametrize("labels", [["release", "gate-check"], ["release", "e2e"]])
def test_a_label_is_not_a_verdict(labels: list[str]) -> None:
    result = rg.decide_release(BUMP, labels, "", [])
    assert result is not None and result[0] is False


def test_a_pin_fails_a_release_pr_even_with_a_passed_gate() -> None:
    result = rg.decide_release(BUMP, [], "success", ["x is pinned"])
    assert result is not None
    passes, message = result
    assert passes is False
    assert "Unreleased SDK pin" in message and "/gate-check" in message


def test_the_newest_gate_check_from_the_bot_wins() -> None:
    rows = [
        _row("gate-check", "success", "a-maintainer"),
        _row("gate-check", "failure", ACTIONS_BOT),
        _row("gate-check", "success", ACTIONS_BOT),
    ]
    row = rg.newest_status(rows, "gate-check", ACTIONS_BOT)
    assert row is not None and row["state"] == "failure"


@pytest.mark.parametrize("creator", ["a-maintainer", "renovate[bot]", APP])
def test_a_gate_check_from_anyone_else_is_not_a_verdict(creator: str) -> None:
    """FND-3947: only the identity test-commands posts with may pass it."""
    assert (
        rg.newest_status(
            [_row("gate-check", "success", creator)], "gate-check", ACTIONS_BOT
        )
        is None
    )


def test_unparseable_labels_read_as_none() -> None:
    assert rg.parse_labels("not json") == []
    assert rg.parse_labels('{"release": true}') == []
    assert rg.parse_labels('["release", 3]') == ["release"]


def test_the_gate_reads_the_context_test_commands_writes() -> None:
    text = (SCRIPTS / "pr_test_command.py").read_text()
    match = re.search(r'^GATE_CHECK_CONTEXT = "([^"]+)"$', text, re.M)
    assert match is not None and match.group(1) == rg.GATE_CHECK_CONTEXT


def test_the_trusted_gate_check_creator_is_the_token_test_commands_posts_with() -> None:
    """`github.token` posts as github-actions[bot]. If test-commands ever
    switches to an App token, every release PR blocks here until this moves."""
    reusable = REPO_ROOT / ".github/workflows/test-commands-reusable.yaml"
    steps = [
        step
        for job in yaml.safe_load(reusable.read_text())["jobs"].values()
        for step in job.get("steps", [])
        if "gate_check_verdict.py" in step.get("run", "")
    ]
    assert steps and all(s["env"]["GH_TOKEN"] == "${{ github.token }}" for s in steps)
    assert rg.VERDICT_CREATOR == ACTIONS_BOT


# -- Unreleased SDK pin ------------------------------------------------------

_REGISTRY = '{ registry = "https://pypi.org/simple" }'
_GIT = '{ git = "https://github.com/example/sdk?rev=abc#abc" }'


def _lock(name: str, version: str, source: str) -> str:
    return (
        "version = 1\n\n"
        '[[package]]\nname = "example-app"\nversion = "1.0.0"\n'
        'source = { virtual = "." }\n\n'
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        f"source = {source}\n"
    )


def _pins(
    tmp_path: pathlib.Path, lock: str | None, pyproject: str | None = None
) -> list[str]:
    lock_file = tmp_path / "uv.lock"
    project_file = tmp_path / "pyproject.toml"
    if lock is not None:
        lock_file.write_text(lock)
    if pyproject is not None:
        project_file.write_text(pyproject)
    return rg.unreleased_sdk_pins(str(lock_file), str(project_file))


def test_a_released_sdk_from_an_index_is_not_a_pin(tmp_path: pathlib.Path) -> None:
    assert _pins(tmp_path, _lock("atlan-application-sdk", "3.57.0", _REGISTRY)) == []


@pytest.mark.parametrize(
    "source", [_GIT, '{ path = "../application-sdk" }', '{ editable = "../sdk" }']
)
def test_an_sdk_locked_off_an_index_is_a_pin(
    tmp_path: pathlib.Path, source: str
) -> None:
    found = _pins(tmp_path, _lock("atlan-application-sdk", "3.57.0", source))
    assert len(found) == 1 and "atlan-application-sdk is locked from a" in found[0]


@pytest.mark.parametrize("version", ["3.58.0rc1", "4.0.0a2", "3.58.0.dev4"])
def test_a_prerelease_sdk_is_a_pin(tmp_path: pathlib.Path, version: str) -> None:
    found = _pins(tmp_path, _lock("atlan-application-sdk", version, _REGISTRY))
    assert found == [f"atlan-application-sdk is locked to pre-release {version}"]


def test_a_post_release_is_released(tmp_path: pathlib.Path) -> None:
    lock = _lock("atlan-application-sdk", "3.57.0.post1", _REGISTRY)
    assert _pins(tmp_path, lock) == []


def test_another_package_from_git_is_not_this_gates_business(
    tmp_path: pathlib.Path,
) -> None:
    assert _pins(tmp_path, _lock("query-redaction", "0.1.0rc1", _GIT)) == []


def test_a_uv_sources_redirect_is_a_pin(tmp_path: pathlib.Path) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        'atlan-application-sdk = { git = "https://github.com/example/sdk" }\n'
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    assert _pins(tmp_path, lock, pyproject) == [
        "atlan-application-sdk is redirected by [tool.uv.sources]"
    ]


def test_a_named_index_array_is_not_a_pin(tmp_path: pathlib.Path) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        "atlan-application-sdk = [\n"
        '  { index = "internal", marker = "sys_platform == \'linux\'" },\n'
        "]\n"
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    assert _pins(tmp_path, lock, pyproject) == []


def test_the_sdk_repo_itself_is_never_a_finding(tmp_path: pathlib.Path) -> None:
    pyproject = '[project]\nname = "atlan-application-sdk"\n'
    lock = _lock("atlan-application-sdk", "3.58.0", '{ editable = "." }')
    assert _pins(tmp_path, lock, pyproject) == []


def test_no_lock_has_nothing_to_check_but_a_broken_one_fails(
    tmp_path: pathlib.Path,
) -> None:
    assert _pins(tmp_path, None) == []
    assert _pins(tmp_path, "this is [not toml") == [rg.LOCK_UNREADABLE]


# ---- the connector reviewer's verdict --------------------------------------


def test_the_contexts_and_identity_are_the_reviewers() -> None:
    """A rename on the reviewer's side would block every pull request."""
    assert rg.REVIEW_CONTEXT == STATUS_CONTEXT
    assert bot_login() == APP


def test_never_reviewed_blocks_with_comment_review() -> None:
    facts = _facts([])
    assert rg.decide_review(facts, HEAD) == (False, rg.REVIEW_MISSING)
    assert "`/review`" in rg.REVIEW_MISSING


def test_green_review_passes() -> None:
    facts = _facts([_row(STATUS_CONTEXT, "success", APP, "ready to merge")])
    passes, message = rg.decide_review(facts, HEAD)
    assert passes and "green" in message


def test_pending_says_still_running() -> None:
    facts = _facts([_row(STATUS_CONTEXT, "pending", APP, "reviewing")])
    assert rg.decide_review(facts, HEAD) == (False, rg.REVIEW_PENDING)
    assert "still running" in rg.REVIEW_PENDING


def test_error_says_rerun_review() -> None:
    facts = _facts([_row(STATUS_CONTEXT, "error", APP, "review incomplete")])
    passes, message = rg.decide_review(facts, HEAD)
    assert not passes
    assert "re-run `/review`" in message and "review incomplete" in message


def test_failure_names_the_open_findings() -> None:
    state = PRState(
        reviewed_head=HEAD,
        round=1,
        findings=[
            _finding("F-aaaaaa", "high"),
            _finding("F-bbbbbb", "low"),
            _finding("F-cccccc", "medium", status="fixed"),
        ],
    )
    facts = _facts([_row(STATUS_CONTEXT, "failure", APP)], state)
    passes, message = rg.decide_review(facts, HEAD)
    assert not passes
    assert "Fix or dismiss the open findings: F-aaaaaa, F-bbbbbb" in message
    assert "F-cccccc" not in message


def test_failure_without_a_readable_state_still_blocks() -> None:
    facts = _facts([_row(STATUS_CONTEXT, "failure", APP, "2 blocking")])
    passes, message = rg.decide_review(facts, HEAD)
    assert not passes and "2 blocking" in message


def test_a_waiver_for_this_head_passes_and_names_who() -> None:
    state = PRState(
        round=1,
        waiver={"by": "a-maintainer", "reason": "accepted risk", "head": HEAD},
        findings=[_finding("F-aaaaaa", "high")],
    )
    rows = [
        _row(STATUS_CONTEXT, "success", APP, "waived by @a-maintainer: accepted risk")
    ]
    passes, message = rg.decide_review(_facts(rows, state), HEAD)
    assert passes
    assert message == "Waived by @a-maintainer for this head: accepted risk"


def test_a_waiver_for_another_head_is_not_this_heads() -> None:
    """A push after a waive moves the head: the new head has no status, and
    the old waiver does not cover it."""
    state = PRState(round=1, waiver={"by": "x", "reason": "r", "head": OTHER})
    assert rg.decide_review(_facts([], state), HEAD) == (False, rg.REVIEW_MISSING)


@pytest.mark.parametrize("creator", ["a-maintainer", ACTIONS_BOT, "renovate[bot]"])
def test_a_review_status_from_anyone_else_is_not_a_verdict(creator: str) -> None:
    """Anyone with write access can post the context; only the App's counts."""
    facts = _facts([_row(STATUS_CONTEXT, "success", creator)])
    assert rg.decide_review(facts, HEAD) == (False, rg.REVIEW_MISSING)


def test_a_forged_newer_success_does_not_mask_the_real_failure() -> None:
    rows = [
        _row(STATUS_CONTEXT, "success", "a-maintainer"),
        _row(STATUS_CONTEXT, "failure", APP),
    ]
    assert rg.decide_review(_facts(rows), HEAD)[0] is False


def test_the_newest_app_status_wins() -> None:
    rows = [_row(STATUS_CONTEXT, "success", APP), _row(STATUS_CONTEXT, "pending", APP)]
    assert rg.decide_review(_facts(rows), HEAD)[0] is True


def test_an_exempt_pr_passes_with_why() -> None:
    found = Exemption(DOCS, "all 2 changed file(s) are Markdown")
    passes, message = rg.decide_review(_facts([], None, found), HEAD)
    assert passes
    assert message.startswith("Exempt from connector review (docs only)")


def test_a_reviewed_pr_is_never_exempt_again() -> None:
    """As in review.run: a forced review that found something must not be
    papered over by the classifier on the next head."""
    found = Exemption(DOCS, "docs")
    state = PRState(round=1, findings=[_finding("F-aaaaaa")])
    facts = _facts([], state, found)
    assert facts.exemption is None
    assert rg.decide_review(facts, HEAD) == (False, rg.REVIEW_MISSING)


def test_the_classifier_is_not_asked_when_there_is_a_verdict() -> None:
    def classify() -> Exemption:
        raise AssertionError("must not compute a diff for a verdict")

    facts = rg.review_facts(
        rows=[_row(STATUS_CONTEXT, "failure", APP)],
        reviewer_login=APP,
        load_state=lambda: None,
        classify=classify,
    )
    assert facts.state == "failure"


def test_an_exempt_class_does_not_override_a_red_verdict() -> None:
    """A forced review of an exempt-class pull request is still its verdict."""
    found = Exemption(DOCS, "docs")
    facts = _facts([_row(STATUS_CONTEXT, "failure", APP)], None, found)
    assert rg.decide_review(facts, HEAD)[0] is False


def test_an_unreadable_review_fails_closed() -> None:
    facts = rg.ReviewFacts(unreadable="HTTP 502")
    passes, message = rg.decide_review(facts, HEAD)
    assert not passes and "HTTP 502" in message


# ---- both duties together --------------------------------------------------


def _decide(head_ref: str, gate: str, review: rg.ReviewFacts) -> rg.Outcome:
    return rg.decide(head_ref, [], gate, [], review, HEAD)


def test_a_version_bump_needs_gate_check_but_no_review() -> None:
    bump = rg.ReviewFacts(exemption=(VERSION_BUMP, "the branch is a version bump"))
    assert _decide(BUMP, "success", bump).passes
    blocked = _decide(BUMP, "", bump)
    assert not blocked.passes
    assert any("/gate-check" in line for line in blocked.failed)
    # The review duty passed: it is not reported as a second failure.
    assert not any(line.startswith("Connector review") for line in blocked.failed)


def test_an_ordinary_pr_has_only_the_review_duty() -> None:
    outcome = _decide("feat/x", "", rg.ReviewFacts())
    assert not outcome.passes
    assert outcome.lines == (f"Connector review: {rg.REVIEW_MISSING}",)


def test_a_green_review_does_not_pass_a_release_pr_without_gate_check() -> None:
    green = rg.ReviewFacts(state="success")
    assert not _decide(BUMP, "", green).passes


def test_the_classifier_is_the_reviewers_own() -> None:
    """`gather` asks connector_reviewer.review.exemption with config.toml's
    lists, the same call `/review` makes, not a copy of the classifier."""
    text = (SCRIPTS / "release_gate.py").read_text()
    assert "reviewer.exemption(" in text
    assert "load_config(" in text
    assert "def classify(" in text  # the local name is a closure, not a copy
    tree = ast.parse(text)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "exemption" not in defined and "_docs" not in defined


# ---- the summary -----------------------------------------------------------


def test_the_summary_lists_open_findings_and_suggestions() -> None:
    state = PRState(
        round=1,
        findings=[
            _finding(
                "F-aaaaaa",
                "high",
                title="A | pipe\nand a newline",
                rule_id="RP02",
                suggestion="Use `x` instead",
            ),
            _finding("F-bbbbbb", "low", rule_id=""),
        ],
        advisories=["Bump the contract version."],
    )
    facts = _facts([_row(STATUS_CONTEXT, "failure", APP)], state)
    outcome = rg.decide("feat/x", [], "", [], facts, HEAD)
    summary = rg.render_summary(outcome, facts)
    assert "Release Gate failed" in summary
    assert (
        "| F-aaaaaa | high | A \\| pipe and a newline | RP02 | app/x.py:12 |" in summary
    )
    assert "| F-bbbbbb | low |" in summary and "| - |" in summary
    assert "**F-aaaaaa**: Use `x` instead" in summary
    assert "Bump the contract version." in summary
    assert "/review dismiss" in summary and "/review waive" in summary


def test_a_passing_summary_lists_no_findings() -> None:
    state = PRState(round=1, findings=[_finding("F-aaaaaa", "high")])
    rows = [_row(STATUS_CONTEXT, "success", APP, "waived by @x: y")]
    facts = _facts(rows, state)
    summary = rg.render_summary(rg.decide("feat/x", [], "", [], facts, HEAD), facts)
    assert "Release Gate passed" in summary and "Open findings" not in summary


def test_emit_writes_the_summary_and_annotates_a_failure(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    outcome = rg.Outcome(
        False,
        ("Release gate: no", "Connector review: yes"),
        ("Release gate: no",),
    )
    assert rg.emit(outcome, rg.ReviewFacts(), str(summary)) == 1
    out = capsys.readouterr().out
    assert "::error title=Release Gate::Release gate: no" in out
    assert "::error title=Release Gate::Connector review" not in out
    assert "Connector review: yes" in out
    text = summary.read_text()
    assert "Release Gate failed" in text
    assert "- ❌ Release gate: no" in text and "- ✅ Connector review: yes" in text
    assert rg.emit(rg.Outcome(True, ("ok",)), rg.ReviewFacts(), "") == 0


# ---- re-run on the verdict -------------------------------------------------


def test_only_the_apps_review_status_reruns() -> None:
    assert rg.should_rerun(STATUS_CONTEXT, APP, APP)
    assert not rg.should_rerun(STATUS_CONTEXT, "a-maintainer", APP)
    assert not rg.should_rerun("gate-check", APP, APP)
    assert not rg.should_rerun("e2e", APP, APP)


def test_rerun_uses_the_gate_check_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """One re-run implementation for both verdicts: gate_check_verdict's."""
    calls: list[tuple[str, str, datetime | None]] = []

    def rerun(github: GitHub, sha: str, at: datetime | None) -> bool:
        calls.append((github.repo, sha, at))
        return True

    monkeypatch.setattr(gate_check_verdict, "rerun_release_gate", rerun)
    argv = ["rerun", "--repo", "o/r", "--sha", HEAD, "--context", STATUS_CONTEXT]
    assert rg.main([*argv, "--creator", APP, "--at", "2026-11-25T10:00:00Z"]) == 0
    assert calls and calls[0][:2] == ("o/r", HEAD)
    assert calls[0][2] == datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
    calls.clear()
    assert rg.main([*argv, "--creator", "someone"]) == 0
    assert calls == []


def test_a_rerun_that_cannot_be_made_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed re-run must fail visibly: the gate would otherwise keep its
    stale verdict with nothing on record."""

    def rerun(github: GitHub, sha: str, at: datetime | None) -> bool:
        raise GitHubError("Release Gate run 1 did not finish")

    monkeypatch.setattr(gate_check_verdict, "rerun_release_gate", rerun)
    argv = ["rerun", "--repo", "o/r", "--sha", HEAD, "--context", STATUS_CONTEXT]
    assert rg.main([*argv, "--creator", APP]) == 1
    assert "::error::Release Gate run 1 did not finish" in capsys.readouterr().err


# ---- reading GitHub: the trust and fail-closed boundary of `gather` --------


class _Rows:
    """Stands in for pr_test_command.GitHub: only `status_rows` is read."""

    rows: list[dict[str, Any]] = []
    error: str = ""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def status_rows(self, sha: str) -> list[dict[str, Any]]:
        if self.error:
            raise GitHubError(self.error)
        return self.rows


def _comment(login: str, state: PRState) -> dict[str, Any]:
    body = f"{SUMMARY_MARKER}\nsummary\n{state.encode()}"
    return {"id": 1, "user": {"login": login}, "body": body}


def _gather(
    monkeypatch: pytest.MonkeyPatch,
    *,
    comments: list[dict[str, Any]] | None = None,
    comments_status: int = 200,
    rows: list[dict[str, Any]] | None = None,
    rows_error: str = "",
) -> rg.ReviewFacts:
    """Run `gather` with the statuses and the reviewer's REST reads stubbed.
    The pull request's head has moved on, so the classifier answers None
    without a diff."""

    def transport(method: str, path: str, body: object, accept: str) -> tuple[int, str]:
        if "/issues/7/comments" in path:
            return comments_status, json.dumps(comments or [])
        if path.endswith("/pulls/7"):
            return 200, json.dumps({"head": {"sha": OTHER}, "base": {"sha": OTHER}})
        return 404, "{}"

    monkeypatch.setattr(_Rows, "rows", rows or [])
    monkeypatch.setattr(_Rows, "error", rows_error)
    monkeypatch.setattr(pr_test_command, "GitHub", _Rows)
    monkeypatch.setattr(
        reviewer_github,
        "GitHub",
        lambda repo: _REAL_REVIEWER_GITHUB(repo, transport=transport),
    )
    _, facts = rg.gather("o/r", 7, HEAD, "")
    return facts


def test_gather_trusts_only_the_apps_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A state block in anyone else's comment is not the reviewer's state:
    it can neither mark the PR reviewed nor supply findings."""
    state = PRState(round=1, findings=[_finding("F-aaaaaa", "high")])
    forged = _gather(monkeypatch, comments=[_comment("a-maintainer", state)])
    assert not forged.reviewed and forged.findings == ()
    real = _gather(monkeypatch, comments=[_comment(APP, state)])
    assert real.reviewed and [f.id for f in real.findings] == ["F-aaaaaa"]


def test_gather_fails_closed_when_the_state_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _gather(monkeypatch, comments_status=502)
    assert facts.unreadable and "502" in facts.unreadable
    assert rg.decide_review(facts, HEAD)[0] is False


def test_gather_fails_closed_when_the_statuses_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _gather(monkeypatch, rows_error="HTTP 500")
    assert facts.unreadable == "HTTP 500"
    assert rg.decide_review(facts, HEAD)[0] is False


# ---- the shim --------------------------------------------------------------


@pytest.fixture(scope="module")
def shim() -> Any:
    return yaml.safe_load(render("release-gate.yaml"))


def _steps(shim: Any, job: str) -> list[Any]:
    return shim["jobs"][job]["steps"]


def _sdk_checkout(shim: Any, job: str) -> Any:
    return next(
        s["with"]
        for s in _steps(shim, job)
        if s.get("with", {}).get("repository") == "atlanhq/application-sdk"
    )


@pytest.mark.parametrize("job", ["release-gate", "rerun-on-review"])
def test_the_gate_runs_from_application_sdk_main(shim: Any, job: str) -> None:
    """FND-5321: the decision is checked out of application-sdk at `main`
    (docs/standards/ci.md, the same pinning rule as
    test_sdk_checkout_ref_pinning), and every `run:` is in that checkout."""
    checkout = _sdk_checkout(shim, job)
    assert checkout["ref"] == "main"
    assert checkout["persist-credentials"] is False
    runs = [s for s in _steps(shim, job) if "run" in s]
    assert runs and all(s["working-directory"] == "sdk/.github/scripts" for s in runs)
    assert all(s["run"].startswith("python3 release_gate.py ") for s in runs)


@pytest.mark.parametrize("job", ["release-gate", "rerun-on-review"])
def test_nothing_of_the_pull_request_is_checked_out(shim: Any, job: str) -> None:
    """Under `pull_request_target` the token can write, so the pull request is
    never checked out (actions/checkout also refuses that pattern there): the
    only checkout is application-sdk, and the pin files are read through the
    API as data."""
    checkouts = [
        s.get("with", {})
        for s in _steps(shim, job)
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    assert all(c.get("repository") == "atlanhq/application-sdk" for c in checkouts)
    assert all("ref" in c and "head" not in str(c["ref"]) for c in checkouts)
    run = _steps(shim, job)[-1]["run"]
    assert "--lock-file" not in run and "--pyproject" not in run


def test_the_vendored_script_is_retired() -> None:
    dest = ".github/scripts/release_gate.py"
    assert dest in RETIRED_FILES and dest in RETIRED_ACTION_FILES
    assert dest not in {d for d, _ in MANAGED_ACTION_FILES}
    with pytest.raises(FileNotFoundError):
        render("release_gate.py")


def test_the_check_name_is_the_required_context(shim: Any) -> None:
    """Rulesets require the bare `Release Gate`: the gate stays an inline job
    (a reusable call would report `<job> / Release Gate`)."""
    job = shim["jobs"]["release-gate"]
    assert job["name"] == "Release Gate" and "uses" not in job
    assert job["if"] == "github.event_name == 'pull_request_target'"


def test_triggers(shim: Any) -> None:
    on = shim[True]
    assert on["pull_request_target"]["types"] == ["opened", "synchronize", "reopened"]
    assert on["pull_request_target"]["branches"] == ["main"]
    assert "merge_group" in on and "status" in on
    # `pull_request` would run the pull request's own copy of the shim.
    assert "pull_request" not in on


def test_the_rerun_listens_only_to_the_reviewer(shim: Any) -> None:
    job = shim["jobs"]["rerun-on-review"]
    assert job["if"] == (
        "github.event_name == 'status' && github.event.context == 'connector-reviewer'"
    )
    env = _steps(shim, "rerun-on-review")[-1]["env"]
    assert env["SHA"] == "${{ github.event.sha }}"
    assert env["CREATOR"] == "${{ github.event.sender.login }}"


def test_least_privilege(shim: Any) -> None:
    assert shim["permissions"] == {"contents": "read"}
    assert shim["jobs"]["release-gate"]["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "issues": "read",
        "statuses": "read",
    }
    assert shim["jobs"]["rerun-on-review"]["permissions"] == {"actions": "write"}


def _sparse(shim: Any, job: str) -> list[str]:
    return _sdk_checkout(shim, job)["sparse-checkout"].split()


@pytest.mark.parametrize(
    ("job", "argv"),
    [
        ("release-gate", ["gate", "--help"]),
        ("rerun-on-review", ["rerun", "--help"]),
    ],
)
def test_the_sparse_checkout_holds_everything_the_job_imports(
    shim: dict, job: str, argv: list[str], tmp_path: pathlib.Path
) -> None:
    """Copy only what the shim checks out and run the same entry point: a
    module the gate imports but the checkout leaves out would fail every run."""
    for entry in _sparse(shim, job):
        src = REPO_ROOT / entry
        dst = tmp_path / entry
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    code = (
        "import release_gate as rg, sys\n"
        "import connector_reviewer.review, connector_reviewer.config\n"
        + ("import gate_check_verdict\n" if job == "rerun-on-review" else "")
        + "import pr_test_command\n"
    )
    scripts = tmp_path / ".github" / "scripts"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=scripts,
        capture_output=True,
        text=True,
        check=False,
        env={},  # no PYTHONPATH: only the copied files are importable
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "release_gate.py", *argv],
        cwd=scripts,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_config_dir_is_the_checked_out_one(shim: Any) -> None:
    assert ".github/connector-reviewer/config.toml" in _sparse(shim, "release-gate")
    env = _steps(shim, "release-gate")[-1]["env"]
    assert env["CONFIG_DIR"] == "${{ github.workspace }}/sdk/.github/connector-reviewer"
    assert env["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_concurrency_is_per_pull_request(shim: Any) -> None:
    """Under `pull_request_target`, `github.ref` is the base branch every PR
    shares: keyed on it, one PR's push would cancel every other PR's gate."""
    group = shim["concurrency"]["group"]
    assert "github.event.pull_request.number" in group
    assert "github.ref" not in group
    assert "pull_request_target" in shim["concurrency"]["cancel-in-progress"]


# ---- pins at the head, through the API -------------------------------------


def test_head_pins_reads_both_files_at_the_head() -> None:
    files = {
        "uv.lock": _lock("atlan-application-sdk", "3.58.0rc1", _REGISTRY),
        "pyproject.toml": '[project]\nname = "example-app"\n',
    }
    found = rg.head_pins(files.get)
    assert found == ["atlan-application-sdk is locked to pre-release 3.58.0rc1"]


def test_head_pins_without_a_lock_has_nothing_to_check() -> None:
    assert rg.head_pins(lambda path: None) == []


def test_head_pins_fails_closed_when_a_file_cannot_be_read() -> None:
    def read(path: str) -> str | None:
        raise GitHubError("HTTP 502")

    found = rg.head_pins(read)
    assert len(found) == 1 and found[0].startswith(rg.PINS_UNREADABLE)
    result = rg.decide_release(BUMP, [], "success", found)
    assert result is not None and result[0] is False


# ---- the sticky PR comment -------------------------------------------------


def _comment_for(outcome: rg.Outcome, review: rg.ReviewFacts) -> str:
    return rg.render_comment(
        outcome, review, HEAD, "https://github.com/o/r/actions/runs/1"
    )


def test_the_comment_says_comment_review_when_never_reviewed() -> None:
    review = rg.ReviewFacts()
    body = _comment_for(rg.decide("feat/x", [], "", [], review, HEAD), review)
    assert body.startswith(rg.COMMENT_MARKER)
    assert "❌ **Release Gate is blocking" in body
    assert "comment `/review`" in body
    assert "[this run](https://github.com/o/r/actions/runs/1)" in body


def test_the_comment_lists_open_findings_with_sanitised_titles() -> None:
    hostile = "Bad <!-- release-gate-verdict --> @someone [x](http://e) | *b*"
    state = PRState(round=1, findings=[_finding("F-aaaaaa", "high", title=hostile)])
    review = _facts([_row(STATUS_CONTEXT, "failure", APP)], state)
    body = _comment_for(rg.decide("feat/x", [], "", [], review, HEAD), review)
    assert "**Open findings (1)**" in body and "`F-aaaaaa` · high ·" in body
    # The marker appears once, at the top: a finding title cannot plant one.
    assert body.count(rg.COMMENT_MARKER) == 1
    assert "<!--" not in body.split("\n", 1)[1]
    assert "@someone" not in body and "@\u200bsomeone" in body
    assert "[x](http://e)" not in body


def test_the_comment_waits_while_the_review_runs() -> None:
    review = _facts([_row(STATUS_CONTEXT, "pending", APP)])
    body = _comment_for(rg.decide("feat/x", [], "", [], review, HEAD), review)
    assert body.splitlines()[1].startswith("⏳")
    assert "still running" in body


def test_the_comment_turns_green_and_names_the_waiver() -> None:
    state = PRState(
        round=1, waiver={"by": "a-maintainer", "reason": "ok", "head": HEAD}
    )
    review = _facts(
        [_row(STATUS_CONTEXT, "success", APP, "waived by @a-maintainer: ok")], state
    )
    body = _comment_for(rg.decide("feat/x", [], "", [], review, HEAD), review)
    assert body.splitlines()[1] == "✅ **Release Gate passed**"
    assert "Waived by @\u200ba-maintainer" in body


def test_the_comment_carries_the_gate_check_reason_on_a_release_pr() -> None:
    review = rg.ReviewFacts(
        exemption=(VERSION_BUMP, "the branch `bump-version-main` is a version bump")
    )
    body = _comment_for(rg.decide(BUMP, [], "", [], review, HEAD), review)
    assert "- ❌ Release gate: Release PR CI is opt-in. Comment /gate-check" in body
    assert "- ✅ Connector review: Exempt from connector review (version bump)" in body
    assert "`bump-version-main`" in body


def test_a_hostile_branch_name_in_a_code_span_is_defused() -> None:
    line = rg._line("the branch `x@y<!--` is a version bump")
    assert "`" not in line and "&lt;!--" in line and "@\u200by" in line


class _Comments:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.writes: list[tuple[str, Any]] = []

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self.comments

    def edit_comment(self, comment_id: int, body: str) -> None:
        self.writes.append(("edit", comment_id))

    def comment(self, number: int, body: str) -> int:
        self.writes.append(("post", number))
        return 1


def test_the_comment_is_posted_once_then_edited_in_place() -> None:
    body = f"{rg.COMMENT_MARKER}\nnew\n"
    gh = _Comments([])
    assert rg.upsert_comment(gh, 7, body) == "created"
    assert gh.writes == [("post", 7)]
    old = {
        "id": 42,
        "user": {"login": rg.COMMENT_AUTHOR},
        "body": f"{rg.COMMENT_MARKER}\nold",
    }
    gh = _Comments([old])
    assert rg.upsert_comment(gh, 7, body) == "updated"
    assert gh.writes == [("edit", 42)]
    gh = _Comments([{**old, "body": body}])
    assert rg.upsert_comment(gh, 7, body) == "unchanged" and gh.writes == []


def test_a_planted_marker_comment_is_never_edited() -> None:
    """Anyone can post a comment containing the marker; only the gate's own
    (github-actions[bot]) is the sticky one."""
    planted = {"id": 5, "user": {"login": "a-maintainer"}, "body": rg.COMMENT_MARKER}
    gh = _Comments([planted])
    assert rg.upsert_comment(gh, 7, f"{rg.COMMENT_MARKER}\nx") == "created"
    assert gh.writes == [("post", 7)]


@pytest.mark.parametrize("missing", ["--head-ref", "--labels"])
def test_the_release_identifiers_are_required(missing: str) -> None:
    """lens F-2c4f21: a call without them must not read as a non-release PR."""
    argv = {
        "--repo": "o/r",
        "--pr": "7",
        "--head-sha": HEAD,
        "--head-ref": BUMP,
        "--labels": "[]",
    }
    del argv[missing]
    flat = [x for pair in argv.items() for x in pair]
    with pytest.raises(SystemExit) as raised:
        rg.main(["gate", *flat])
    assert raised.value.code == 2


def test_the_shim_passes_both_release_identifiers(shim: Any) -> None:
    run = _steps(shim, "release-gate")[-1]["run"]
    assert '--head-ref "$HEAD_REF"' in run and '--labels "$LABELS"' in run

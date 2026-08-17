"""Tests for the best-effort comment-reaction helper.

The regression these pin: on 2026-08-17 a transient `HTTP 503` from
`POST /issues/comments/{id}/reactions` failed the `sdk-review-dispatch` job
outright — the reaction was the job's first API call and an unhandled
rejection in `actions/github-script` fails the step. Two `@sdk-review`
requests were dropped before the mothership session was ever created.

So the assertions here are mostly about what does *not* happen: a failing
reaction never propagates a non-zero exit, and a transient one gets a second
chance before it is given up on.

There is also a cross-file guard at the bottom asserting no workflow reaches
for the reactions API inline again, which is the shape the bug had.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location(
    "react_to_comment", Path(__file__).resolve().parents[1] / "react_to_comment.py"
)
react_mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(react_mod)


REPO = "atlanhq/application-sdk"
COMMENT_ID = "5318632555"

# Verbatim from the failed run (job 95463483353), minus the response dump.
GH_503 = (
    "gh: No server is currently available to service your request. "
    "Sorry about that. Please try resubmitting your request and contact us "
    "if the problem persists. (HTTP 503)"
)
GH_403_PERMS = "gh: Resource not accessible by integration (HTTP 403)"
GH_403_RATE = "gh: API rate limit exceeded for user ID 62283865. (HTTP 403)"
GH_404 = "gh: Not Found (HTTP 404)"
GH_422 = "gh: Validation Failed (HTTP 422)"


def ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def fail(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


class RecordingRunner:
    """A `subprocess.run` stand-in that replays a scripted list of results."""

    def __init__(self, *results: subprocess.CompletedProcess):
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if not self._results:
            raise AssertionError(f"unexpected extra invocation: {args}")
        return self._results.pop(0)


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --------------------------------------------------------------------------
# Transience classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        GH_503,
        "gh: Bad gateway (HTTP 502)",
        "gh: Gateway Timeout (HTTP 504)",
        "gh: Internal Server Error (HTTP 500)",
        "gh: Too Many Requests (HTTP 429)",
        GH_403_RATE,
        "gh: You have exceeded a secondary rate limit",
        "error connecting to api.github.com: connection reset by peer",
        "Post https://api.github.com/...: unexpected EOF",
    ],
)
def test_transient_failures_are_retryable(stderr: str) -> None:
    assert react_mod.is_transient(stderr) is True


@pytest.mark.parametrize("stderr", [GH_403_PERMS, GH_404, GH_422])
def test_terminal_failures_are_not_retryable(stderr: str) -> None:
    assert react_mod.is_transient(stderr) is False


def test_permission_403_is_not_confused_with_a_throttle_403() -> None:
    """Both are 403; only one clears on its own.

    A token missing `issues: write` fails identically on every attempt, so
    retrying it just spends the job's budget to reach the same answer.
    """
    assert react_mod.is_transient(GH_403_RATE) is True
    assert react_mod.is_transient(GH_403_PERMS) is False


# --------------------------------------------------------------------------
# Retry behaviour
# --------------------------------------------------------------------------


def test_first_attempt_success_makes_one_call() -> None:
    runner = RecordingRunner(ok())
    sleeper = RecordingSleeper()

    assert (
        react_mod.react(REPO, COMMENT_ID, "eyes", runner=runner, sleeper=sleeper)
        is True
    )
    assert len(runner.calls) == 1
    assert sleeper.delays == []


def test_the_503_that_broke_dispatch_is_retried_and_succeeds() -> None:
    runner = RecordingRunner(fail(GH_503), ok())
    sleeper = RecordingSleeper()

    assert (
        react_mod.react(REPO, COMMENT_ID, "eyes", runner=runner, sleeper=sleeper)
        is True
    )
    assert len(runner.calls) == 2
    assert sleeper.delays == [2.0]


def test_backoff_doubles_and_stops_at_the_attempt_cap() -> None:
    runner = RecordingRunner(fail(GH_503), fail(GH_503), fail(GH_503))
    sleeper = RecordingSleeper()

    assert (
        react_mod.react(
            REPO,
            COMMENT_ID,
            "eyes",
            max_attempts=3,
            backoff_seconds=2,
            runner=runner,
            sleeper=sleeper,
        )
        is False
    )
    assert len(runner.calls) == 3
    # Two waits for three attempts — never a trailing sleep after the last try,
    # which would just burn job time before giving up anyway.
    assert sleeper.delays == [2.0, 4.0]


def test_terminal_failure_stops_immediately() -> None:
    runner = RecordingRunner(fail(GH_403_PERMS))
    sleeper = RecordingSleeper()

    assert (
        react_mod.react(REPO, COMMENT_ID, "eyes", runner=runner, sleeper=sleeper)
        is False
    )
    assert len(runner.calls) == 1
    assert sleeper.delays == []


def test_the_request_targets_the_comment_reactions_endpoint() -> None:
    runner = RecordingRunner(ok())
    react_mod.react(REPO, COMMENT_ID, "rocket", runner=runner, sleeper=lambda _: None)

    args = runner.calls[0]
    assert args[:2] == ["gh", "api"]
    assert f"repos/{REPO}/issues/comments/{COMMENT_ID}/reactions" in args
    assert "content=rocket" in args
    assert "POST" in args


# --------------------------------------------------------------------------
# The exit-code contract — the actual point of the script
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [GH_503, GH_403_PERMS, GH_404, GH_422, "totally unrecognised failure"],
)
def test_main_always_exits_zero_however_the_reaction_fails(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", COMMENT_ID)
    monkeypatch.setenv("REACTION", "eyes")
    monkeypatch.setenv("REACT_BACKOFF_SECONDS", "0.001")
    monkeypatch.setattr(react_mod.subprocess, "run", lambda *a, **k: fail(stderr))

    assert react_mod.main() == 0


def test_main_no_ops_without_a_comment_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """workflow_dispatch has no comment; the caller may still invoke this."""
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", "")

    def explode(*a, **k):
        raise AssertionError("should not have called gh")

    monkeypatch.setattr(react_mod.subprocess, "run", explode)
    assert react_mod.main() == 0


def test_main_rejects_a_reaction_github_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", COMMENT_ID)
    monkeypatch.setenv("REACTION", "thumbsup")  # the API name is '+1'

    def explode(*a, **k):
        raise AssertionError("should not have called gh")

    monkeypatch.setattr(react_mod.subprocess, "run", explode)
    assert react_mod.main() == 0


@pytest.mark.parametrize("raw", ["", "nonsense", "0", "-4"])
def test_malformed_tuning_env_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("REACT_MAX_ATTEMPTS", raw)
    assert (
        react_mod._positive_int("REACT_MAX_ATTEMPTS", react_mod.DEFAULT_MAX_ATTEMPTS)
        == react_mod.DEFAULT_MAX_ATTEMPTS
    )


# --------------------------------------------------------------------------
# Cross-file guard: no workflow may react inline again
# --------------------------------------------------------------------------

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
ACTIONS_DIR = Path(__file__).resolve().parents[3] / ".github" / "actions"
SCRIPT_NAME = "react_to_comment.py"

# The Octokit call this script replaced. Matched as raw text rather than by
# walking the parsed YAML because it lives inside `github-script`'s JS body,
# which YAML sees as an opaque string.
INLINE_REACTION = re.compile(r"reactions\.createForIssueComment")


def _yaml_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.y*ml"))
    files += sorted(ACTIONS_DIR.glob("*/action.y*ml"))
    assert files, "no workflow YAML found — the guard is pointed at the wrong path"
    return files


def test_no_workflow_reacts_inline() -> None:
    """An inline reaction is an unguarded API call in the middle of a job.

    Reads the tree rather than a checked-in list, so a newly added entry point
    that reaches for the reactions API directly fails here instead of quietly
    reintroducing the single point of failure.
    """
    offenders = [
        path.relative_to(WORKFLOW_DIR.parents[1]).as_posix()
        for path in _yaml_files()
        if INLINE_REACTION.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "these call the reactions API inline; route them through "
        f".github/scripts/{SCRIPT_NAME} instead: {offenders}"
    )


def test_every_caller_can_actually_reach_the_script() -> None:
    """A caller that never checks out the repo would fail at `python3 …`.

    Cheap to get wrong: two of these workflows react *before* their main
    checkout, so they carry a sparse one just for this file.
    """
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8")
        if SCRIPT_NAME not in text:
            continue
        doc = yaml.safe_load(text)
        jobs = doc.get("jobs") or {}
        for job_name, job in jobs.items():
            steps = job.get("steps") or []
            checked_out = False
            for step in steps:
                uses = str(step.get("uses") or "")
                run = str(step.get("run") or "")
                if uses.startswith("actions/checkout@"):
                    checked_out = True
                if SCRIPT_NAME in run:
                    assert checked_out, (
                        f"{path.name}:{job_name} runs {SCRIPT_NAME} with no "
                        "preceding actions/checkout"
                    )


def test_callers_pass_the_env_the_script_reads() -> None:
    """`REPO` and `COMMENT_ID` are read from env, so a typo is silent.

    Without `REPO` the script warns and no-ops; without `GH_TOKEN` every
    attempt 401s. Both look like "the emoji just didn't appear".
    """
    required = {"GH_TOKEN", "REPO", "COMMENT_ID"}
    seen_callers = 0
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8")
        if SCRIPT_NAME not in text:
            continue
        doc = yaml.safe_load(text)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if SCRIPT_NAME not in str(step.get("run") or ""):
                    continue
                seen_callers += 1
                env = set(step.get("env") or {})
                missing = required - env
                assert not missing, (
                    f"{path.name}:{job_name} step "
                    f"{step.get('name')!r} is missing env {sorted(missing)}"
                )
    assert seen_callers >= 5, (
        "expected every bot entry point to route through the helper; "
        f"found only {seen_callers}"
    )

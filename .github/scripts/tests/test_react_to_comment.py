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
import math
import re
import subprocess
from pathlib import Path, PurePosixPath

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


def test_a_runner_that_raises_warns_and_returns_false() -> None:
    """A missing/non-executable `gh` raises before any CompletedProcess exists.

    `subprocess.run` raises FileNotFoundError when the binary is absent; the
    always-exit-0 contract has to hold for that too, not just for non-zero
    return codes — an exception escaping `main()` fails the step exactly the
    way the unguarded 503 did.
    """

    def missing_gh(*args, **kwargs) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "No such file or directory", "gh")

    assert (
        react_mod.react(
            REPO, COMMENT_ID, "eyes", runner=missing_gh, sleeper=RecordingSleeper()
        )
        is False
    )


def test_a_sleeper_that_raises_warns_and_returns_false() -> None:
    """Same boundary one call later: the wait between attempts must not kill
    the job either."""

    def broken_sleeper(_seconds: float) -> None:
        raise OverflowError("timestamp out of range for platform time_t")

    assert (
        react_mod.react(
            REPO,
            COMMENT_ID,
            "eyes",
            runner=RecordingRunner(fail(GH_503)),
            sleeper=broken_sleeper,
        )
        is False
    )


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
    monkeypatch.setattr(react_mod, "_RUN", lambda *a, **k: fail(stderr))

    assert react_mod.main() == 0


def test_main_exits_zero_even_when_the_runner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end for the spawn-failure path: `gh` absent → warning, exit 0."""
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", COMMENT_ID)
    monkeypatch.setenv("REACTION", "eyes")

    def missing_gh(*args, **kwargs) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(react_mod, "_RUN", missing_gh)

    assert react_mod.main() == 0


def test_main_no_ops_without_a_comment_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """workflow_dispatch has no comment; the caller may still invoke this."""
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", "")

    def explode(*a, **k):
        raise AssertionError("should not have called gh")

    monkeypatch.setattr(react_mod, "_RUN", explode)
    assert react_mod.main() == 0


def test_main_rejects_a_reaction_github_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", COMMENT_ID)
    monkeypatch.setenv("REACTION", "thumbsup")  # the API name is '+1'

    def explode(*a, **k):
        raise AssertionError("should not have called gh")

    monkeypatch.setattr(react_mod, "_RUN", explode)
    assert react_mod.main() == 0


@pytest.mark.parametrize("raw", ["", "nonsense", "0", "-4", "inf", "nan"])
def test_malformed_attempt_count_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("REACT_MAX_ATTEMPTS", raw)
    assert (
        react_mod._positive_int("REACT_MAX_ATTEMPTS", react_mod.DEFAULT_MAX_ATTEMPTS)
        == react_mod.DEFAULT_MAX_ATTEMPTS
    )


@pytest.mark.parametrize(
    "raw", ["", "nonsense", "0", "-4", "inf", "-inf", "Infinity", "nan"]
)
def test_malformed_backoff_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """`float('inf') > 0` is True, so a naive positivity check lets it through.

    It would then reach `time.sleep(inf)`, which raises OverflowError and exits
    non-zero — the always-exit-0 contract broken by the script's own config
    parsing, from an env var a caller could set to anything.
    """
    monkeypatch.setenv("REACT_BACKOFF_SECONDS", raw)
    assert (
        react_mod._positive_float(
            "REACT_BACKOFF_SECONDS", react_mod.DEFAULT_BACKOFF_SECONDS
        )
        == react_mod.DEFAULT_BACKOFF_SECONDS
    )


def test_non_finite_backoff_never_reaches_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through `main`, which wires the real `time.sleep`.

    The stand-in raises on a non-finite delay the way `time.sleep` does, so
    this fails if the guard is removed rather than passing on a mock that
    tolerates anything.
    """
    slept: list[float] = []

    def strict_sleep(seconds: float) -> None:
        if not math.isfinite(seconds):
            raise OverflowError("cannot convert float infinity to integer")
        slept.append(seconds)

    monkeypatch.setenv("REPO", REPO)
    monkeypatch.setenv("COMMENT_ID", COMMENT_ID)
    monkeypatch.setenv("REACTION", "eyes")
    monkeypatch.setenv("REACT_BACKOFF_SECONDS", "inf")
    monkeypatch.setenv("REACT_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(react_mod, "_RUN", lambda *a, **k: fail(GH_503))
    monkeypatch.setattr(react_mod, "_SLEEP", strict_sleep)

    assert react_mod.main() == 0
    assert slept == [react_mod.DEFAULT_BACKOFF_SECONDS]


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
    checkout, so they carry a sparse one just for this file — and, since
    FND-637, at a `path:` of its own so the sparse setting cannot leak into the
    full checkout that follows. That makes the *path* part of the contract too:
    the `run:` line has to name the directory the sparse checkout landed in, or
    `python3` fails on a file that is genuinely not there. Asserting only that
    "some checkout happened first" would not catch that.
    """
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8")
        if SCRIPT_NAME not in text:
            continue
        doc = yaml.safe_load(text)
        jobs = doc.get("jobs") or {}
        for job_name, job in jobs.items():
            steps = job.get("steps") or []
            checkout_dirs: set[str] = set()
            for step in steps:
                uses = str(step.get("uses") or "")
                run = str(step.get("run") or "")
                if uses.startswith("actions/checkout@"):
                    with_block = step.get("with") or {}
                    raw = str(with_block.get("path") or ".").strip().rstrip("/")
                    checkout_dirs.add(raw or ".")
                if SCRIPT_NAME in run:
                    assert checkout_dirs, (
                        f"{path.name}:{job_name} runs {SCRIPT_NAME} with no "
                        "preceding actions/checkout"
                    )
                    invoked = [
                        token for token in run.split() if token.endswith(SCRIPT_NAME)
                    ]
                    assert invoked, (
                        f"{path.name}:{job_name} names {SCRIPT_NAME} but the "
                        "guard could not find the invoked path in the run body"
                    )
                    for token in invoked:
                        parent = str(PurePosixPath(token).parent)
                        # `.github/scripts/x.py` → checked out at the root;
                        # `.ack/.github/scripts/x.py` → checked out at `.ack`.
                        prefix = parent[: -len(".github/scripts")].rstrip("/") or "."
                        assert prefix in checkout_dirs, (
                            f"{path.name}:{job_name} runs '{token}', which needs "
                            f"a checkout at '{prefix}', but this job only checks "
                            f"out at {sorted(checkout_dirs)}"
                        )


# Every bot entry point that acknowledges a comment, as (workflow, job, step).
#
# Pinned exactly rather than counted. A `>= 5` lower bound would stay green if
# one of these lost its ack while an unrelated sixth caller was added
# elsewhere — the count balances, the entry point goes silent, and the guard
# that exists to notice says nothing. Adding a genuinely new caller is
# supposed to require editing this list.
EXPECTED_CALLERS = {
    ("sdk-review.yml", "react-on-skip", "React to skipped trigger"),
    ("sdk-review.yml", "sdk-review-dispatch", "React to comment"),
    ("sdk-review.yml", "sdk-review-dispatch", "React to skipped re-trigger"),
    ("sdk-resolve.yml", "sdk-resolve-dispatch", "Acknowledge with a reaction"),
    ("auto-fix-vulnerabilities.yaml", "auto-fix", "React to comment"),
    ("capability-manifest-regen.yaml", "regen", "React to comment"),
    ("sdk-loop.yml", "fence", "React to the trigger comment"),
}


def _caller_steps() -> dict[tuple[str, str, str], dict]:
    """Every `(workflow, job, step name)` that invokes the helper."""
    found: dict[tuple[str, str, str], dict] = {}
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8")
        if SCRIPT_NAME not in text:
            continue
        doc = yaml.safe_load(text)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if SCRIPT_NAME in str(step.get("run") or ""):
                    found[(path.name, job_name, str(step.get("name")))] = step
    return found


def test_the_caller_set_is_exactly_what_we_expect() -> None:
    actual = set(_caller_steps())
    assert actual == EXPECTED_CALLERS, (
        f"lost acks: {sorted(EXPECTED_CALLERS - actual)}; "
        f"new callers to add to EXPECTED_CALLERS: {sorted(actual - EXPECTED_CALLERS)}"
    )


def test_callers_pass_the_env_the_script_reads() -> None:
    """`REPO` and `COMMENT_ID` are read from env, so a typo is silent.

    Without `REPO` the script warns and no-ops; without `GH_TOKEN` every
    attempt 401s. Both look like "the emoji just didn't appear".
    """
    required = {"GH_TOKEN", "REPO", "COMMENT_ID"}
    for caller, step in _caller_steps().items():
        missing = required - set(step.get("env") or {})
        assert not missing, f"{caller} is missing env {sorted(missing)}"


def test_every_caller_workflow_grants_issues_write() -> None:
    """The reactions endpoint needs `issues: write`, even for a PR comment.

    `/issues/comments/{id}/reactions` is not covered by `pull-requests:
    write`. Two of these workflows were missing it. Now that the helper warns
    and exits 0 instead of failing the step, a permissions trim here would
    silence an ack permanently with nothing red to show for it — which is
    precisely why this is asserted rather than left to be noticed.
    """
    for path in {WORKFLOW_DIR / name for name, _, _ in EXPECTED_CALLERS}:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        permissions = doc.get("permissions") or {}
        assert permissions.get("issues") == "write", (
            f"{path.name} reacts to comments but grants "
            f"issues={permissions.get('issues')!r}"
        )

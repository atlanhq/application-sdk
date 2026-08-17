"""Guards for the merge-group path of the `commits.yaml` reusable (FND-381).

`Conventional Commits` is meant to be safe to make a REQUIRED check in a
connector repo that uses a merge queue. Two independent things have to hold for
that, and neither is exercisable here by a runner:

1. **The caller must dispatch on `merge_group`.** A required check that is never
   dispatched for the merge-group event produces no conclusion at all, and the
   queue entry sits pending forever rather than failing — strictly worse than a
   red check. The bootstrap shim template is the only place a connector can get
   this from (hand-adding it locally trips C002 drift), so the trigger is pinned
   there.

2. **The reusable must conclude `success` on that event.** There is no PR title
   to inspect, so every validating/commenting step is `pull_request`-gated and a
   final no-op step carries the conclusion. If the no-op's gate were ever
   narrowed — or the fail step's widened — a merge-group entry would either have
   no successful step or go red on an empty verdict.

Both are GitHub-evaluated `if:` expressions, so each gate is lifted verbatim out
of the YAML and evaluated against synthetic contexts — the same approach as
test_trivy_action_gates.py and test_label_trigger_gates.py, and for the same
reason: a presence check proves a term is there, not that it is wired in at the
right precedence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REUSABLE = _REPO_ROOT / ".github/workflows/commits.yaml"
_SHIM = _REPO_ROOT / "packages/conformance/conformance/bootstrap/templates/commits.yaml"

#: Steps that need a PR title and must therefore be skipped on merge_group.
_PR_ONLY_STEPS = (
    "Checkout SDK helper scripts",
    "Validate PR title",
    "Post sticky comment on violation",
    "Clear sticky comment when resolved",
)

#: The step that carries the conclusion when there is no PR title.
_NOOP_STEP = "Non-PR event no-op"

#: The step that turns a violation into a failure.
_FAIL_STEP = "Fail on invalid PR title"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps() -> list[dict[str, Any]]:
    return _load(_REUSABLE)["jobs"]["conventional-commits"]["steps"]


def _gate(name: str) -> str:
    for step in _steps():
        if step.get("name") == name:
            gate = step.get("if")
            assert gate, f"step {name!r} lost its `if:` gate"
            return str(gate)
    raise AssertionError(f"{_REUSABLE.name} has no step named {name!r}")


def _contexts(*, event: str, violation: str, fork: bool = False) -> dict[str, Any]:
    """Synthetic contexts for one event.

    ``violation`` is the *string* a step output holds — `""` on merge_group,
    where the step that would set it never ran.
    """
    return {
        "github": {
            "event_name": event,
            "event": {"pull_request": {"head": {"repo": {"fork": fork}}}},
        },
        "steps": {"detect": {"outputs": {"violation": violation}}},
    }


# ── 1. The shim dispatches on merge_group ────────────────────────────────────


def test_shim_declares_merge_group() -> None:
    """Without this the check is never dispatched for a queue entry, and a
    required check with no conclusion wedges the queue instead of failing it."""
    # `on` is parsed by PyYAML 1.1 rules, where a bare `on:` key is the boolean
    # True — hence the fallback rather than a plain ["on"].
    shim = _load(_SHIM)
    triggers = shim.get("on", shim.get(True))
    assert triggers is not None, "shim lost its `on:` block"
    assert "merge_group" in triggers


def test_shim_still_dispatches_on_edited() -> None:
    """Re-titling a flagged PR has to re-run the check and clear its comment."""
    shim = _load(_SHIM)
    triggers = shim.get("on", shim.get(True))
    assert "edited" in triggers["pull_request"]["types"]


# ── 2. The reusable concludes success on merge_group ─────────────────────────


@pytest.mark.parametrize("name", _PR_ONLY_STEPS)
def test_pr_only_steps_are_skipped_on_merge_group(name: str) -> None:
    gate = _gate(name)
    assert evaluate(gate, _contexts(event="merge_group", violation="")) is False


#: Each PR-only step paired with the verdict that makes it the applicable one:
#: checkout/validate run either way, post-comment only on a violation, and
#: clear-comment only once the title is fixed.
_PR_ONLY_STEPS_WITH_VERDICT = (
    ("Checkout SDK helper scripts", ""),
    ("Validate PR title", ""),
    ("Post sticky comment on violation", "true"),
    ("Clear sticky comment when resolved", "false"),
)


@pytest.mark.parametrize("name, violation", _PR_ONLY_STEPS_WITH_VERDICT)
def test_pr_only_steps_run_on_a_non_fork_pull_request(
    name: str, violation: str
) -> None:
    gate = _gate(name)
    assert evaluate(gate, _contexts(event="pull_request", violation=violation)) is True


def test_every_pr_only_step_has_a_verdict_case() -> None:
    """Keeps the two lists from drifting apart when a step is added."""
    assert tuple(name for name, _ in _PR_ONLY_STEPS_WITH_VERDICT) == _PR_ONLY_STEPS


# ── Coverage: the classifications are pinned to the YAML, not to each other ──


def test_every_step_is_named() -> None:
    """An unnamed step is invisible to every check below, which look steps up by
    name — so the coverage assertion would silently stop covering it."""
    for index, step in enumerate(_steps()):
        assert step.get("name"), f"step {index} in {_REUSABLE.name} has no `name:`"


def test_every_step_is_classified() -> None:
    """Anchors coverage to the workflow itself.

    Comparing the classification lists only to each other proves they agree, not
    that they describe the job: a step added to the YAML without a
    classification would pass every test in this file while breaking the
    merge_group or fork design it was never checked against.
    """
    named = {str(step["name"]) for step in _steps()}
    classified = set(_PR_ONLY_STEPS) | {_NOOP_STEP, _FAIL_STEP}
    assert named == classified, (
        "unclassified step(s): "
        f"{sorted(named - classified)}; stale classification(s): "
        f"{sorted(classified - named)}"
    )


def test_every_step_is_gated() -> None:
    """Every step in this job is conditional on the event or the verdict. An
    ungated one would run on merge_group, where there is no PR title to act on
    and no verdict to read."""
    for step in _steps():
        assert step.get("if"), f"step {step['name']!r} has no `if:` gate"


# ── The sticky comment cannot be raced by an overlapping run ─────────────────


def test_reusable_serialises_runs_per_pr() -> None:
    """Both comment paths mutate one sticky comment, so an older run's POST
    landing after a newer run's CLEAR would leave an obsolete failure comment on
    a passing check. A per-PR group with cancel-in-progress is what prevents it."""
    concurrency = _load(_REUSABLE)["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_noop_step_runs_on_merge_group() -> None:
    """The one step that must carry the conclusion when there is no PR title."""
    gate = _gate(_NOOP_STEP)
    assert evaluate(gate, _contexts(event="merge_group", violation="")) is True


def test_noop_step_is_skipped_on_a_pull_request() -> None:
    gate = _gate(_NOOP_STEP)
    assert evaluate(gate, _contexts(event="pull_request", violation="false")) is False


def test_fail_step_is_skipped_on_merge_group() -> None:
    """An empty verdict must not be read as a violation — a merge-group entry
    would go red on a title that already passed at PR time."""
    gate = _gate(_FAIL_STEP)
    assert evaluate(gate, _contexts(event="merge_group", violation="")) is False


def test_fail_step_fires_on_a_real_violation() -> None:
    gate = _gate(_FAIL_STEP)
    assert evaluate(gate, _contexts(event="pull_request", violation="true")) is True


# ── Fork PRs: verdict still gates, commenting does not ──────────────────────


@pytest.mark.parametrize(
    "name",
    ("Post sticky comment on violation", "Clear sticky comment when resolved"),
)
def test_comment_steps_are_skipped_on_a_fork_pr(name: str) -> None:
    """Fork PRs get a read-only token, so commenting would fail the job."""
    gate = _gate(name)
    assert (
        evaluate(gate, _contexts(event="pull_request", violation="true", fork=True))
        is False
    )


def test_fail_step_still_fires_on_a_fork_pr() -> None:
    """Skipping the comment must not skip the gate."""
    gate = _gate(_FAIL_STEP)
    assert (
        evaluate(gate, _contexts(event="pull_request", violation="true", fork=True))
        is True
    )


# ── This repo polices the PR title, never the branch's commits ───────────────
#
# application-sdk is squash-merge-only, with the squash subject taken from the
# PR title and the squash body left blank, so a branch's individual commit
# subjects are discarded at merge. A per-commit convention gate here blocks on
# text that cannot reach main, the changelog, or the version bump — and it did:
# a correctly-titled PR stayed red on a WIP commit subject. Dropping it is only
# safe while the title itself is still gated, so both halves of that trade are
# pinned here rather than left to a comment.

_PR_CHECKS = _REPO_ROOT / ".github/workflows/pull_request.yaml"
_TITLE_GUARD = _REPO_ROOT / ".github/workflows/pr-title-convention.yaml"


def test_pr_checks_does_not_gate_individual_commit_subjects() -> None:
    """Tripwire for the string form plus a structural check on the jobs: a
    per-commit gate reintroduced as a shell `run:` step (no action name to
    match) must still fail here."""
    body = _PR_CHECKS.read_text(encoding="utf-8")
    assert "action-conventional-commits" not in body, (
        "PR Checks gates every commit subject on the branch again. Those "
        "subjects are dropped by the squash merge, so this can only ever fail "
        "a PR whose merged history would have been fine. Gate the PR title "
        "instead — pr-title-convention.yaml already does."
    )
    for job_id, job in _load(_PR_CHECKS)["jobs"].items():
        assert "conventional-commit" not in job_id.lower(), (
            f"PR Checks job {job_id!r} reintroduces a per-commit convention "
            "gate. Branch commit subjects are dropped by the squash merge — "
            "gate the PR title instead (pr-title-convention.yaml already does)."
        )
        assert "conventional-commit" not in str(job.get("name", "")).lower(), (
            f"PR Checks job {job_id!r} is named {job['name']!r}, reintroducing "
            "a per-commit convention gate. Gate the PR title instead."
        )


def test_the_pr_title_is_still_gated() -> None:
    """The removal above is conditional on this check existing — and actually
    running: a workflow that kept the job name while losing its trigger or its
    validation steps would leave nothing policing the squash subject."""
    guard = _load(_TITLE_GUARD)
    jobs = guard["jobs"]
    names = {job.get("name") for job in jobs.values()}
    assert "Validate PR title" in names, (
        "pr-title-convention.yaml no longer publishes a 'Validate PR title' "
        "check. With the per-commit gate gone, nothing validates the string "
        "that becomes the squash subject — which drives both the changelog "
        "and the release version bump."
    )
    triggers = guard.get("on", guard.get(True))
    assert triggers is not None and "pull_request" in triggers, (
        "pr-title-convention.yaml no longer triggers on pull_request, so the "
        "'Validate PR title' job above would never run for a PR."
    )
    job = next(job for job in jobs.values() if job.get("name") == "Validate PR title")
    step_names = [step.get("name") for step in job.get("steps", [])]
    assert "Validate PR title against changed files" in step_names, (
        "The 'Validate PR title' job lost its validation step — the check "
        "would pass every title."
    )
    assert "Fail on invalid PR title" in step_names, (
        "The 'Validate PR title' job lost its fail step — a violation would "
        "no longer turn the check red."
    )

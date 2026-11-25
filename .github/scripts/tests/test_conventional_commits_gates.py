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

2. **The reusable must pass on that event.** There is no PR title to inspect,
   so the job carries a job-level `pull_request` gate and is *skipped* there.
   A skipped job still files its check run, and GitHub counts `skipped` as a
   pass for a required check; it also costs no runner minute, which a job that
   started only to skip every step did (FND-3320). If that gate were ever
   widened, the job would run on merge_group with an empty event payload.

Both are GitHub-evaluated `if:` expressions, so each gate is lifted verbatim out
of the YAML and evaluated against synthetic contexts — the same approach as
test_trivy_action_gates.py and test_label_trigger_gates.py, and for the same
reason: a presence check proves a term is there, not that it is wired in at the
right precedence.
"""

from __future__ import annotations

from typing import Any

import pytest
from _gha_expr import evaluate
from _workflows import (
    REPO_ROOT,
    WORKFLOWS,
    job,
    load_workflow,
    step_named,
    steps,
    triggers,
)

_REUSABLE = WORKFLOWS / "commits.yaml"
_SHIM = REPO_ROOT / "packages/conformance/conformance/bootstrap/templates/commits.yaml"

#: Steps that run on every pull_request the job runs for.
_UNGATED_STEPS = ("Checkout SDK helper scripts", "Validate PR title")

#: Steps that act on the verdict, each paired with the verdict that applies it.
_VERDICT_STEPS_WITH_VERDICT = (
    ("Post sticky comment on violation", "true"),
    ("Clear sticky comment when resolved", "false"),
)

#: The step that turns a violation into a failure.
_FAIL_STEP = "Fail on invalid PR title"


#: The reusable's one job.
_JOB = "conventional-commits"


def _gate(name: str) -> str:
    gate = step_named(job(load_workflow(_REUSABLE), _JOB), name).get("if")
    assert gate, f"step {name!r} lost its `if:` gate"
    return str(gate)


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
    assert "merge_group" in triggers(load_workflow(_SHIM))


def test_shim_still_dispatches_on_edited() -> None:
    """Re-titling a flagged PR has to re-run the check and clear its comment."""
    assert "edited" in triggers(load_workflow(_SHIM))["pull_request"]["types"]


# ── 2. The reusable is skipped, at job level, on merge_group ────────────────


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch"])
def test_job_is_skipped_off_the_pull_request_event(event: str) -> None:
    """A job-level skip files a `skipped` check run — a pass for a required
    check — and bills nothing. A started job that skipped every step billed a
    full runner minute on every queue entry."""
    gate = str(job(load_workflow(_REUSABLE), _JOB)["if"])
    assert evaluate(gate, _contexts(event=event, violation="")) is False


def test_job_runs_on_a_pull_request() -> None:
    gate = str(job(load_workflow(_REUSABLE), _JOB)["if"])
    assert evaluate(gate, _contexts(event="pull_request", violation="")) is True


def test_job_name_is_static() -> None:
    """The check-run name is the required context. An expression in it would
    file the skipped run under a different name than the PR run, and the
    required context would sit at "Expected" on the queue entry."""
    assert "${{" not in str(job(load_workflow(_REUSABLE), _JOB)["name"])


@pytest.mark.parametrize("name", _UNGATED_STEPS)
def test_validation_steps_run_on_every_pull_request(name: str) -> None:
    """The job gate already selects the event; a step gate here could only
    narrow which PRs get a verdict."""
    step = step_named(job(load_workflow(_REUSABLE), _JOB), name)
    assert not step.get("if"), f"step {name!r} regained a step-level gate"


@pytest.mark.parametrize("name, violation", _VERDICT_STEPS_WITH_VERDICT)
def test_verdict_steps_run_on_a_non_fork_pull_request(
    name: str, violation: str
) -> None:
    gate = _gate(name)
    assert evaluate(gate, _contexts(event="pull_request", violation=violation)) is True


# ── Coverage: the classifications are pinned to the YAML, not to each other ──


def test_every_step_is_named() -> None:
    """An unnamed step is invisible to every check below, which look steps up by
    name — so the coverage assertion would silently stop covering it."""
    for index, step in enumerate(steps(job(load_workflow(_REUSABLE), _JOB))):
        assert step.get("name"), f"step {index} in {_REUSABLE.name} has no `name:`"


def test_every_step_is_classified() -> None:
    """Anchors coverage to the workflow itself.

    Comparing the classification lists only to each other proves they agree, not
    that they describe the job: a step added to the YAML without a
    classification would pass every test in this file while breaking the
    merge_group or fork design it was never checked against.
    """
    named = {str(step["name"]) for step in steps(job(load_workflow(_REUSABLE), _JOB))}
    classified = (
        set(_UNGATED_STEPS)
        | {name for name, _ in _VERDICT_STEPS_WITH_VERDICT}
        | {_FAIL_STEP}
    )
    assert named == classified, (
        "unclassified step(s): "
        f"{sorted(named - classified)}; stale classification(s): "
        f"{sorted(classified - named)}"
    )


# ── The sticky comment cannot be raced by an overlapping run ─────────────────


def test_reusable_serialises_runs_per_pr() -> None:
    """Both comment paths mutate one sticky comment, so an older run's POST
    landing after a newer run's CLEAR would leave an obsolete failure comment on
    a passing check. A per-PR concurrency group is what prevents it."""
    concurrency = load_workflow(_REUSABLE)["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]


def test_reusable_queues_rather_than_cancelling() -> None:
    """Serialise by QUEUING, never by cancelling.

    Cancelling an in-flight run leaves a `cancelled` check run on the head SHA,
    and Renovate greens a branch only when every check run on it is
    success/skipped/neutral — so one cancelled entry pins the branch yellow and
    Renovate's own merge never fires, while GitHub still shows the PR CLEAN
    (it reads the latest run per required context). atlan-cosmosdb-app#97 sat
    green and unmerged for 14 days on exactly this: a reopen plus two
    force-pushes inside 4 seconds put two runs on one SHA, and the first was
    cancelled by the second.

    Queuing keeps the ordering property the test above asserts, at the cost of a
    few seconds of runner time on a ~10s job.
    """
    assert load_workflow(_REUSABLE)["concurrency"]["cancel-in-progress"] is False


def test_fail_step_is_skipped_on_a_clean_title() -> None:
    gate = _gate(_FAIL_STEP)
    assert evaluate(gate, _contexts(event="pull_request", violation="false")) is False


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

_PR_CHECKS = WORKFLOWS / "pull_request.yaml"
_TITLE_GUARD = WORKFLOWS / "pr-title-convention.yaml"


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
    for job_id, body in load_workflow(_PR_CHECKS)["jobs"].items():
        assert "conventional-commit" not in job_id.lower(), (
            f"PR Checks job {job_id!r} reintroduces a per-commit convention "
            "gate. Branch commit subjects are dropped by the squash merge — "
            "gate the PR title instead (pr-title-convention.yaml already does)."
        )
        assert "conventional-commit" not in str(body.get("name", "")).lower(), (
            f"PR Checks job {job_id!r} is named {body['name']!r}, reintroducing "
            "a per-commit convention gate. Gate the PR title instead."
        )


def test_the_pr_title_is_still_gated() -> None:
    """The removal above is conditional on this check existing — and actually
    running: a workflow that kept the job name while losing its trigger or its
    validation steps would leave nothing policing the squash subject."""
    guard = load_workflow(_TITLE_GUARD)
    jobs = guard["jobs"]
    names = {job.get("name") for job in jobs.values()}
    assert "Validate PR title" in names, (
        "pr-title-convention.yaml no longer publishes a 'Validate PR title' "
        "check. With the per-commit gate gone, nothing validates the string "
        "that becomes the squash subject — which drives both the changelog "
        "and the release version bump."
    )
    assert "pull_request" in triggers(guard), (
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

"""conformance-reusable.yaml runs every series in ONE job (FND-3318).

It used to be a 12-leg matrix plus a `Contracts` job and a `Conformance Gate`
job aggregating the legs.  Collapsing that is only safe if the single job
reproduces, per series, what each leg did:

* the same series run — each detect step fires exactly when its old leg's
  detect step did: its own filter matched, or push, or force-all.  Evaluated
  through the real expressions, not string-matched, over every combination;
* a failing series does not stop the ones after it (the matrix was
  fail-fast off), so every series gate carries `!cancelled()`;
* the same SARIF artifacts — `conformance-<slug>-sarif` holding
  `<slug>.sarif`, the names the upload-sarif reusable and
  fetch_conformance_sarif.py resolve;
* the same required context — this job is named `Conformance Gate`, the name
  every fleet ruleset requires (`suite / Conformance Gate`), and nothing can
  make it skip;
* the same verdict for the ledger guard — it was never part of the gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _conformance_series import filters, load_workflow, series, suite_job  # noqa: E402
from _gha_expr import evaluate, evaluate_operand  # noqa: E402

#: Every series the old matrix ran, letter -> slug.  Restated on purpose: the
#: collapse must not drop or rename one, and a rename would orphan the
#: Security-tab category and the dashboard's history for that series.
EXPECTED_SERIES = {
    "C": "ci",
    "E": "error-handling",
    "P": "prescriptions",
    "F": "preflight",
    "O": "optimizations",
    "D": "dependency",
    "L": "logging",
    "T": "tests",
    "I": "container-image",
    "B": "deprecation",
    "K": "contract-toolkit",
    "S": "security",
}

EVENTS = ("pull_request", "merge_group", "push")


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return load_workflow()


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:  # type: ignore[type-arg]
    return suite_job(workflow)


def _step(job: dict, step_id: str) -> dict:  # type: ignore[type-arg]
    matches = [s for s in job["steps"] if s.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}"
    return matches[0]


def _contexts(*, event: str, force_all: bool, outputs: dict[str, str]) -> dict:  # type: ignore[type-arg]
    return {
        "inputs": {"event_name": event, "force-all": force_all},
        "steps": {
            "changes": {"outputs": outputs},
            "apt-packages": {"outcome": "skipped"},
        },
    }


def test_one_job_carries_the_required_context(workflow: dict) -> None:  # type: ignore[type-arg]
    """One job, named as the rulesets require, that can never be skipped."""
    assert list(workflow["jobs"]) == ["suite"], (
        "the suite is one job; a second job is a second billed runner, and a "
        "job the required context waits on must not be able to skip"
    )
    job = workflow["jobs"]["suite"]
    assert job["name"] == "Conformance Gate", (
        "fleet rulesets require `suite / Conformance Gate`; renaming this job "
        "leaves that context unreported and blocks every merge"
    )
    assert "if" not in job, "a job-level `if:` can skip the required context"
    assert "strategy" not in job, "the per-series matrix is what FND-3318 removed"
    assert "needs" not in job


def test_every_series_is_present_once(job: dict) -> None:  # type: ignore[type-arg]
    declared = {entry.letter: entry.slug for entry in series(job)}
    assert declared == EXPECTED_SERIES
    assert len(series(job)) == len(EXPECTED_SERIES), "a series runs twice"


def test_every_series_has_its_own_filter(job: dict) -> None:  # type: ignore[type-arg]
    """One named filter per slug, and no filter that nothing reads."""
    assert set(filters(job)) == set(EXPECTED_SERIES.values())
    for entry in series(job):
        assert entry.globs, f"{entry.letter}-series has no paths filter"


def test_dependency_series_runs_last(job: dict) -> None:  # type: ignore[type-arg]
    """D syncs the caller's env into `.venv/`; any series after it would walk it."""
    assert series(job)[-1].letter == "D"


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("force_all", (False, True))
def test_each_series_runs_exactly_when_its_old_leg_did(
    job: dict,
    event: str,
    force_all: bool,  # type: ignore[type-arg]
) -> None:
    """Each leg ran detect on `relevant == 'true' || push || force-all`, with
    `relevant` its own filter.  For every series, flip only its own filter and
    then every OTHER filter: the first must decide, the second must not."""
    slugs = list(EXPECTED_SERIES.values())
    for entry in series(job):
        for own in ("true", "false", ""):
            for others in ("true", "false"):
                outputs = {slug: others for slug in slugs}
                outputs[entry.slug] = own
                expected = own == "true" or event == "push" or force_all
                got = evaluate(
                    entry.detect["if"],
                    _contexts(event=event, force_all=force_all, outputs=outputs),
                )
                assert got == expected, (
                    f"{entry.letter}-series: event={event} force-all={force_all} "
                    f"own filter={own!r} other filters={others!r} -> runs={got}, "
                    f"the old leg would have run={expected}"
                )


def test_a_failed_series_does_not_stop_the_rest(job: dict) -> None:  # type: ignore[type-arg]
    """Without `!cancelled()` the implicit `success()` skips every series after
    the first red one, and the later series' findings are never reported."""
    for entry in series(job):
        assert "!cancelled()" in " ".join(entry.detect["if"].split())
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        if uses.startswith("actions/upload-artifact@"):
            assert "!cancelled()" in step["if"], step.get("name")


def test_each_series_uploads_its_own_sarif_under_its_old_name(job: dict) -> None:  # type: ignore[type-arg]
    """Same gate as the detect step, same artifact name and file as the leg."""
    for entry in series(job):
        upload = _step(job, f"upload-{entry.slug}")
        assert upload["with"]["name"] == f"conformance-{entry.slug}-sarif"
        assert upload["with"]["path"] == f"{entry.slug}.sarif"
        steps = job["steps"]
        assert steps.index(entry.detect) < steps.index(upload)
        for event in EVENTS:
            for force_all in (False, True):
                for own in ("true", "false"):
                    contexts = _contexts(
                        event=event,
                        force_all=force_all,
                        outputs={entry.slug: own},
                    )
                    assert evaluate(upload["if"], contexts) == evaluate(
                        entry.detect["if"], contexts
                    ), f"{entry.slug}: upload and detect gates disagree"


def test_ledger_guard_never_fails_the_gate(job: dict) -> None:  # type: ignore[type-arg]
    """The ledger guard was its own `Contracts` job, outside the old gate's
    `needs`, and no ruleset requires it.  Folded in, it must stay non-blocking
    and keep its push-to-main skip."""
    for step_id in ("ledger-published", "ledger-ref"):
        step = _step(job, step_id)
        assert step.get("continue-on-error") is True, step_id
        assert "inputs.event_name != 'push'" in step["if"], step_id


def test_full_history_only_where_the_ledger_guard_needs_it(job: dict) -> None:  # type: ignore[type-arg]
    """`0` is falsy in an expression, so the order of `&&`/`||` matters."""
    checkout = job["steps"][0]
    depth = checkout["with"]["fetch-depth"]
    assert evaluate_operand(depth, {"inputs": {"event_name": "push"}}) == 1
    for event in ("pull_request", "merge_group"):
        assert evaluate_operand(depth, {"inputs": {"event_name": event}}) == 0

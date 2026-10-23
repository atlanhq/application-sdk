"""Guards for label-gated job conditions (FND-48).

`contains(github.event.pull_request.labels.*.name, 'x')` asks whether the PR
carries `x` *right now*. That is the right question for `opened`, `synchronize`
and `reopened`, and the wrong one for `labeled`: GitHub has no trigger-level
filter for which label fired, so on a PR that already carries `x` any unrelated
label add — the size/, area/, dependency and review-state labels bots churn
constantly — satisfies the state check and re-runs the job. For the `e2e` gates
that meant a 20–40 minute live-tenant suite, multiplied by the cross-CSP matrix
and queued rather than replaced (`cancel-in-progress: false` is deliberate, so
each spurious add stacked behind the last).

The fix is one extra term per gate:

    (github.event.action != 'labeled' || github.event.label.name == 'x')

FND-3839 then removed the trigger itself: e2e and the bump-PR opt-in are PR
comments (`/e2e`, `/gate-check`), and no workflow or scaffold subscribes to
`labeled` any more (:func:`test_no_test_workflow_or_scaffold_subscribes_to_labeled`).
application-sdk's own e2e jobs follow the `e2e-request` commit status instead
of a label. The reusable's label gates stay, behind `e2e-label-trigger`, for
connector callers that predate the resync — so the FND-48 guards below still
apply to them.

Two layers of guard here, because each catches what the other cannot:

* :func:`test_every_label_gate_reachable_by_a_labeled_event_is_event_aware`
  sweeps every workflow, so a *new* label gate cannot land without the term. It
  is textual, so it proves presence but not correctness.
* The behavioural tests lift each real gate expression out of the YAML and
  evaluate it against synthetic payloads. Those prove the term is wired in at
  the right precedence — `&&` binds tighter than `||` in GHA, so a term added
  one paren out is a silently no-op gate that a presence check would pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pr_test_command  # noqa: E402
from _gha_expr import evaluate as _evaluate  # noqa: E402
from _gha_expr import evaluate_operand  # noqa: E402
from conformance.bootstrap import render as bootstrap_render  # noqa: E402

#: tests-reusable.yaml's inputs as a caller that predates FND-3839 passes them:
#: it still subscribes to `labeled`, so the label gates below must keep working
#: for it until the fleet has resynced. Tests override per case.
_LEGACY_CALLER_INPUTS = {
    "enable-e2e": True,
    "run-e2e": "",
    "head-sha": "",
    "e2e-label-trigger": True,
}


def evaluate(expression: str, contexts: dict[str, Any]) -> Any:
    contexts = dict(contexts)
    contexts["inputs"] = {**_LEGACY_CALLER_INPUTS, **(contexts.get("inputs") or {})}
    return _evaluate(expression, contexts)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github/workflows"
_TEMPLATE_DIR = _REPO_ROOT / "packages/conformance/conformance/bootstrap/templates"

#: The state check that is only half a gate on a `labeled`-triggered workflow.
_LABEL_STATE_CHECK = re.compile(
    r"contains\(\s*github\.event\.pull_request\.labels\.\*\.name\s*,\s*'([^']+)'\s*\)"
)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_or_skip(path: Path) -> dict[str, Any] | None:
    """Load a workflow for the repo-wide sweep, tolerating unparseable files.

    No workflow is currently unparseable. This tolerance was added for
    `.github/workflows/scheduled-trivy-scan.yml`, which did not parse as YAML on
    main (a heredoc body inside a `run: |` block sat at column 0, terminating the
    block scalar) — and which has since been deleted, on the reasoning noted here
    that repairing it would silently start a ticket-filing security scan. GitHub
    cannot run an invalid workflow, so such a file cannot exhibit the bug this
    sweep looks for.

    Kept rather than tightened into an assertion: this sweep's job is the
    label-trigger gate, and failing it on someone else's malformed YAML would
    make it a check born red on unrelated debt — the failure mode that gets a
    guard disabled instead of fixed (cf. test_e2e_tenant_install_workflow.py
    scoping its own guard). `test_artifact_upload_retry.py` owns the assertion
    that no new unparseable workflow lands.
    """
    try:
        return _load(path)
    except yaml.YAMLError:
        return None


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1 truthiness),
    # so a workflow's trigger block is under one key or the other.
    raw = workflow.get("on", workflow.get(True)) or {}
    return raw if isinstance(raw, dict) else {}


def _event_aware_term(label: str) -> re.Pattern[str]:
    return re.compile(
        r"github\.event\.action\s*!=\s*'labeled'\s*\|\|\s*"
        rf"github\.event\.label\.name\s*==\s*'{re.escape(label)}'"
    )


def _conditions(workflow: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield (where, expression) for every job- and step-level `if:`."""
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if "if" in job:
            yield job_name, str(job["if"])
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "if" in step:
                yield f"{job_name} → {step.get('name', '?')}", str(step["if"])


def _can_receive_a_labeled_event(workflow: dict[str, Any]) -> bool:
    """Whether a `labeled` action can reach this workflow's gates.

    A reusable (`workflow_call`) workflow cannot see its callers' trigger lists,
    and connectors not yet resynced past FND-3839 still ship `labeled` in their
    `tests.yaml`, so a reusable must always be treated as reachable. A workflow
    with its own `pull_request` trigger is judged on its declared types; one that
    lists only `closed` (the release-on-merge shape) genuinely cannot be hit.
    """
    triggers = _triggers(workflow)
    if "workflow_call" in triggers:
        return True
    pull_request = triggers.get("pull_request")
    if pull_request is None:
        return False
    types = (pull_request or {}).get("types")
    # No `types:` means GitHub's default set, which does not include `labeled`.
    return bool(types) and "labeled" in types


def _rendered_template(path: Path) -> dict[str, Any] | None:
    """A bootstrap shim as a fresh connector repo receives it (default slots).

    The shims ship to every connector, and since FND-3748 several subscribe to
    `labeled` for the bump-PR `gate-check` opt-in, so they need the same guard.
    Template text comes from this checkout: the installed conformance package
    is a non-editable copy that can lag the source.
    """
    with mock.patch.object(
        bootstrap_render,
        "_load_template",
        lambda name: (_TEMPLATE_DIR / name).read_text(encoding="utf-8"),
    ):
        try:
            return yaml.safe_load(bootstrap_render.render(path.name)) or {}
        except yaml.YAMLError:
            return None


def _sweep_targets() -> Iterator[tuple[Path, dict[str, Any] | None]]:
    for path in sorted(_WORKFLOW_DIR.glob("*.y*ml")):
        yield path, _load_or_skip(path)
    for path in sorted(_TEMPLATE_DIR.glob("*.y*ml")):
        workflow = _rendered_template(path)
        # Only the GitHub workflow shims; the dir also holds action.yaml copies.
        if workflow and "jobs" in workflow:
            yield path, workflow


def test_every_label_gate_reachable_by_a_labeled_event_is_event_aware() -> None:
    offenders: list[str] = []
    checked = 0
    for path, workflow in _sweep_targets():
        if workflow is None or not _can_receive_a_labeled_event(workflow):
            continue
        for where, expression in _conditions(workflow):
            flat = " ".join(expression.split())
            for label in _LABEL_STATE_CHECK.findall(flat):
                checked += 1
                if not _event_aware_term(label).search(flat):
                    offenders.append(f"{path.name}: {where} (label {label!r})")
    assert checked, (
        "this guard found no label-gated jobs at all — the detection regex has "
        "drifted from how the gates are written, so it is passing vacuously"
    )
    assert not offenders, (
        "these gates test whether the PR carries the label rather than whether "
        "that label was the one just added, so every unrelated label add re-runs "
        "them (FND-48). Add `(github.event.action != 'labeled' || "
        "github.event.label.name == '<label>')` to the same `&&` chain — and if a "
        "gate genuinely must fire on any label change, say so here with a reason "
        f"rather than leaving it looking like this bug. Offenders: {offenders}"
    )


# ── Behavioural coverage of the real gates ───────────────────────────────────


def _gate(workflow_file: str, job: str) -> str:
    workflow = _load(_WORKFLOW_DIR / workflow_file)
    jobs = workflow.get("jobs") or {}
    assert job in jobs, f"job {job!r} is gone from {workflow_file}; update this guard"
    condition = jobs[job].get("if")
    assert condition, f"{workflow_file}:{job} lost its `if:` gate entirely"
    return str(condition)


def _pull_request(
    action: str,
    labels: tuple[str, ...],
    *,
    added: str | None = None,
    fork: bool = False,
    login: str = "a-human",
) -> dict[str, Any]:
    """A `pull_request` github context, shaped like the real webhook payload."""
    event: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "labels": [{"name": name} for name in labels],
            "head": {"repo": {"fork": fork}},
            "user": {"login": login},
        },
    }
    if added is not None:
        # Only a `labeled`/`unlabeled` payload carries `label`; on every other
        # action the key is simply absent, which is what makes the guard term
        # inert outside the case it exists for.
        event["label"] = {"name": added}
    return {"event_name": "pull_request", "event": event}


def _merge_group() -> dict[str, Any]:
    return {"event_name": "merge_group", "event": {"action": "checks_requested"}}


#: The acceptance table from FND-48, expressed once and reused for every gate.
#: `LABEL` is substituted with the gate's own label.
_SCENARIOS: tuple[tuple[str, str, tuple[str, ...], str | None, bool], ...] = (
    # description,                        action,        labels,       added,       runs
    ("opened carrying the label", "opened", ("LABEL",), None, True),
    ("a real push", "synchronize", ("LABEL",), None, True),
    ("reopened carrying the label", "reopened", ("LABEL",), None, True),
    ("the label being added", "labeled", ("LABEL",), "LABEL", True),
    ("the label re-added after a removal", "labeled", ("LABEL",), "LABEL", True),
    # The regression this ticket exists for.
    (
        "an unrelated label added alongside it",
        "labeled",
        ("LABEL", "size/M"),
        "size/M",
        False,
    ),
    ("an unrelated label on a PR without it", "labeled", ("size/M",), "size/M", False),
    ("opened without the label", "opened", (), None, False),
    ("a push without the label", "synchronize", ("size/M",), None, False),
)


def _scenarios(label: str) -> Iterator[tuple[str, dict[str, Any], bool]]:
    for description, action, labels, added, runs in _SCENARIOS:
        github = _pull_request(
            action,
            tuple(label if name == "LABEL" else name for name in labels),
            added=label if added == "LABEL" else added,
        )
        yield description, github, runs


def _assert_gate(expression: str, label: str, extra: dict[str, Any]) -> None:
    for description, github, expected in _scenarios(label):
        actual = evaluate(expression, {"github": github, **extra})
        assert actual is expected, (
            f"gate for {label!r} on {description}: expected the job to "
            f"{'run' if expected else 'be skipped'}, got the opposite"
        )


# `needs` values that are otherwise-green, so each scenario isolates the label
# behaviour rather than accidentally being skipped by an unrelated term.
_CHANGES_SDK = {
    "changes": {"outputs": {"sdk": "true", "container": "false", "ci": "false"}}
}


def test_discover_e2e_only_reacts_to_the_e2e_label_being_added() -> None:
    """tests-reusable.yaml — the gate 17 connector repos consume."""
    _assert_gate(
        _gate("tests-reusable.yaml", "discover-e2e"),
        "e2e",
        {"inputs": {"enable-e2e": True, "run-e2e": ""}},
    )


# ── application-sdk's PR Checks follow `e2e-request`, not a label (FND-3839) ──
#
# A `/e2e` comment posts an `e2e-request` status on the head commit and re-runs
# the e2e-request job, whose answer every PR-time e2e job reads. Labels are not
# consulted at all, so no label event — there are none any more — and no stale
# label can start a live-tenant suite.

#: Each PR-time e2e job with `needs` that are otherwise green, so a scenario
#: isolates the request rather than being skipped by an unrelated term.
_SDK_E2E_JOBS: dict[str, dict[str, Any]] = {
    "build-sdk-base-image": {},
    "connector-tests": {
        "matrix-builder": {"result": "success"},
        "build-sdk-base-image": {"result": "success"},
        "merge-sdk-base-image": {"result": "success"},
    },
    "sdr-k8s-e2e": {},
    "storage-integration": {},
}


def _sdk_needs(
    job: str, *, requested: str, storage: str = "false", sdk: str = "true"
) -> dict[str, Any]:
    return {
        "changes": {
            "result": "success",
            "outputs": {
                "sdk": sdk,
                "container": "false",
                "ci": "false",
                "storage": storage,
            },
        },
        "e2e-request": {"outputs": {"requested": requested}},
        **_SDK_E2E_JOBS[job],
    }


@pytest.mark.parametrize("job", list(_SDK_E2E_JOBS))
@pytest.mark.parametrize(
    ("requested", "runs"),
    [
        ("true", True),
        ("false", False),
        # e2e-request failed or was skipped: no answer is not a request.
        ("", False),
    ],
)
def test_sdk_e2e_jobs_follow_the_e2e_request(
    job: str, requested: str, runs: bool
) -> None:
    expression = _gate("pull_request.yaml", job)
    github = _pull_request("synchronize", ())
    needs = _sdk_needs(job, requested=requested)
    assert evaluate(expression, {"github": github, "needs": needs}) is runs


@pytest.mark.parametrize("job", list(_SDK_E2E_JOBS))
@pytest.mark.parametrize("labels", [("e2e",), ("gate-check",), ("e2e", "gate-check")])
def test_sdk_e2e_jobs_ignore_labels(job: str, labels: tuple[str, ...]) -> None:
    """A label left on the PR, from before FND-3839 or added by habit, is inert."""
    expression = _gate("pull_request.yaml", job)
    assert "labels.*.name" not in expression
    github = _pull_request("synchronize", labels)
    needs = _sdk_needs(job, requested="false")
    assert evaluate(expression, {"github": github, "needs": needs}) is False


@pytest.mark.parametrize("job", list(_SDK_E2E_JOBS))
def test_sdk_e2e_jobs_wait_for_the_request(job: str) -> None:
    """Re-running e2e-request (what `/e2e` does) re-runs only its dependents;
    a job that does not depend on it would never see the answer change."""
    workflow = _load(_WORKFLOW_DIR / "pull_request.yaml")
    assert "e2e-request" in workflow["jobs"][job]["needs"]


@pytest.mark.parametrize(
    ("storage", "requested", "runs"),
    [
        ("true", "false", True),
        ("false", "true", True),
        ("true", "true", True),
        ("false", "false", False),
    ],
)
def test_storage_integration_runs_on_a_storage_change_or_an_e2e_request(
    storage: str, requested: str, runs: bool
) -> None:
    """FND-1153 kept the real-cloud storage suite off the merge queue (the
    Entra federated credential has no subject for a `gh-readonly-queue/*`
    ref), which left it opt-in via a label nothing applied. FND-3839 runs it
    unasked on a PR that can affect storage, and on any `/e2e` request."""
    expression = _gate("pull_request.yaml", "storage-integration")
    needs = _sdk_needs("storage-integration", requested=requested, storage=storage)
    github = _pull_request("synchronize", ())
    assert evaluate(expression, {"github": github, "needs": needs}) is runs


def test_storage_integration_runs_on_a_storage_change_with_no_sdk_change() -> None:
    """A Markdown-only change under application_sdk/storage/ matches the
    storage filter while sdk_changes_non_md.py turns `sdk` false. A gate that
    regressed to also require `sdk` would pass every case above (they all set
    sdk=true) and silently skip the suite here (lens F-55f377)."""
    expression = _gate("pull_request.yaml", "storage-integration")
    needs = _sdk_needs(
        "storage-integration", requested="false", storage="true", sdk="false"
    )
    github = _pull_request("synchronize", ())
    assert evaluate(expression, {"github": github, "needs": needs}) is True


def test_storage_integration_stays_off_the_merge_queue() -> None:
    expression = _gate("pull_request.yaml", "storage-integration")
    needs = _sdk_needs("storage-integration", requested="true", storage="true")
    assert evaluate(expression, {"github": _merge_group(), "needs": needs}) is False


def test_storage_integration_waits_for_a_successful_changes_job() -> None:
    """`always()` lets it run past a skipped e2e-request; without the explicit
    `changes` result it would also run past a failed path filter."""
    expression = _gate("pull_request.yaml", "storage-integration")
    needs = _sdk_needs("storage-integration", requested="true", storage="true")
    needs["changes"]["result"] = "failure"
    github = _pull_request("synchronize", ())
    assert evaluate(expression, {"github": github, "needs": needs}) is False


# ── FND-851: integration on an `e2e`-labelled PR even under a merge queue ───
#
# `detect-integration` and `integration` share the same event/queue/label
# expression. `_assert_gate` covers the queued-PR case (the label term is live
# there). The rest pins the paths where the label term is inert, and the extra
# `needs` gates that only `integration` carries.


def _queue_needs(*, enabled: bool) -> dict[str, Any]:
    return {
        "needs": {
            "detect-merge-queue": {
                "outputs": {"enabled": "true" if enabled else "false"}
            }
        }
    }


def _integration_needs(
    *,
    enabled: bool,
    result: str = "success",
    count: str = "1",
) -> dict[str, Any]:
    return {
        "needs": {
            "detect-merge-queue": {
                "outputs": {"enabled": "true" if enabled else "false"}
            },
            "detect-integration": {
                "result": result,
                "outputs": {"count": count},
            },
        }
    }


def test_detect_integration_on_a_queued_pr_only_reacts_to_the_e2e_label_being_added() -> (
    None
):
    """Queued PRs skip detection unless they carry `e2e` (FND-851)."""
    _assert_gate(
        _gate("tests-reusable.yaml", "detect-integration"),
        "e2e",
        _queue_needs(enabled=True),
    )


def test_integration_on_a_queued_pr_only_reacts_to_the_e2e_label_being_added() -> None:
    _assert_gate(
        _gate("tests-reusable.yaml", "integration"),
        "e2e",
        _integration_needs(enabled=True),
    )


def test_detect_integration_and_integration_agree_on_when_the_tier_runs() -> None:
    """Detection never runs for a tier that won't, and vice versa.

    The two `if:`s must move together so Tests Gate cannot see a green skip of
    Integration while Detect still ran, or the inverse.
    """
    detect = _gate("tests-reusable.yaml", "detect-integration")
    integration = _gate("tests-reusable.yaml", "integration")
    for enabled in (True, False):
        detect_extra = _queue_needs(enabled=enabled)
        integration_extra = _integration_needs(enabled=enabled)
        for description, github, _expected in _scenarios("e2e"):
            detect_runs = evaluate(detect, {"github": github, **detect_extra})
            integration_runs = evaluate(
                integration, {"github": github, **integration_extra}
            )
            assert detect_runs is integration_runs, (
                f"queued={enabled} {description}: detect={detect_runs} "
                f"integration={integration_runs}"
            )


@pytest.mark.parametrize(
    "github",
    [
        {"event_name": "merge_group", "event": {"action": "checks_requested"}},
        {"event_name": "push", "event": {}},
        {"event_name": "workflow_dispatch", "event": {}},
    ],
    ids=["merge_group", "push", "workflow_dispatch"],
)
def test_integration_always_runs_off_the_pr_path(github: dict[str, Any]) -> None:
    """Non-PR events are the queued/unlabelled cadence; the e2e term must not gate them."""
    detect = _gate("tests-reusable.yaml", "detect-integration")
    integration = _gate("tests-reusable.yaml", "integration")
    # Queue output is absent on these events (`detect-merge-queue` is skipped);
    # the `event_name != 'pull_request'` term must win without reading it. An
    # unproven `queue-proof` (FND-4344) leaves no output either.
    assert evaluate(detect, {"github": github, "needs": {}}) is True
    assert (
        evaluate(
            integration,
            {
                "github": github,
                "needs": {
                    "detect-integration": {
                        "result": "success",
                        "outputs": {"count": "1"},
                    }
                },
            },
        )
        is True
    )


def test_integration_still_runs_on_every_pr_when_there_is_no_merge_queue() -> None:
    """Queue-absent consumers have no merge_group to catch a bad merge."""
    detect = _gate("tests-reusable.yaml", "detect-integration")
    integration = _gate("tests-reusable.yaml", "integration")
    detect_extra = _queue_needs(enabled=False)
    integration_extra = _integration_needs(enabled=False)
    for description, github, _expected in _scenarios("e2e"):
        assert evaluate(detect, {"github": github, **detect_extra}) is True, description
        assert (
            evaluate(integration, {"github": github, **integration_extra}) is True
        ), description


def test_integration_stays_gated_on_successful_detection_and_a_nonzero_suite() -> None:
    """A failed or empty detection cannot start Integration, even on an e2e PR."""
    expression = _gate("tests-reusable.yaml", "integration")
    github = _pull_request("synchronize", ("e2e",))
    assert (
        evaluate(expression, {"github": github, **_integration_needs(enabled=True)})
        is True
    )
    assert (
        evaluate(
            expression,
            {
                "github": github,
                **_integration_needs(enabled=True, result="failure"),
            },
        )
        is False
    )
    assert (
        evaluate(
            expression,
            {
                "github": github,
                **_integration_needs(enabled=True, result="skipped"),
            },
        )
        is False
    )
    assert (
        evaluate(
            expression,
            {"github": github, **_integration_needs(enabled=True, count="0")},
        )
        is False
    )


# ── The paths the fix must not disturb ───────────────────────────────────────


def test_a_workflow_dispatch_with_run_e2e_still_runs_the_suite() -> None:
    """Cross-repo dispatch from an SDK PR never sees a `labeled` action at all."""
    expression = _gate("tests-reusable.yaml", "discover-e2e")
    contexts = {
        "github": {"event_name": "workflow_dispatch", "event": {}},
        "inputs": {"enable-e2e": True, "run-e2e": "true"},
    }
    assert evaluate(expression, contexts) is True
    contexts["inputs"] = {"enable-e2e": True, "run-e2e": "false"}
    assert evaluate(expression, contexts) is False
    # enable-e2e is the connector's opt-out and must still win outright.
    contexts["inputs"] = {"enable-e2e": False, "run-e2e": "true"}
    assert evaluate(expression, contexts) is False


#: GHA's status-check functions. A job `if:` that calls none of them gets an
#: implicit `success() &&`, which a SKIPPED need fails — before the rest of the
#: expression is even looked at.
_STATUS_FUNCTION = re.compile(r"\b(always|success|failure|cancelled)\(\)")


def _merge_queue_needs(changed: str, *, changes_result: str = "success") -> dict:
    outputs = {"sdk": "false", "container": "false", "ci": "false"}
    outputs[changed] = "true"
    return {
        "changes": {"result": changes_result, "outputs": outputs},
        "matrix-builder": {"result": "success"},
        # Skipped on the merge-queue path — the gate tolerates that by design.
        "build-sdk-base-image": {"result": "skipped"},
        "merge-sdk-base-image": {"result": "skipped"},
        # pull_request only, so always skipped here.
        "e2e-request": {"result": "skipped", "outputs": {}},
    }


@pytest.mark.parametrize("job", ["connector-tests", "sdr-k8s-e2e"])
def test_merge_queue_e2e_survives_the_skipped_e2e_request(job: str) -> None:
    """merge_group runs e2e unconditionally, and e2e-request is pull_request
    only, so it is ALWAYS skipped in the queue.

    A job that `needs` it without calling a status function gets GitHub's
    implicit `success()`, which a skipped need fails: the job is skipped
    before its `if:` is read, and the queue merges with no e2e at all. That
    is what sdr-k8s-e2e shipped with. The evaluator cannot see the implicit
    term, so the expression evaluating true proves nothing on its own — the
    status function has to be pinned explicitly.
    """
    workflow = _load(_WORKFLOW_DIR / "pull_request.yaml")
    needs = workflow["jobs"][job]["needs"]
    assert "e2e-request" in needs
    expression = _gate("pull_request.yaml", job)
    assert _STATUS_FUNCTION.search(expression), (
        f"{job} needs the always-skipped-in-the-queue e2e-request but calls no "
        "status function, so GitHub skips it on every merge_group run"
    )
    for changed in ("sdk", "container", "ci"):
        contexts = {"github": _merge_group(), "needs": _merge_queue_needs(changed)}
        assert evaluate(expression, contexts) is True, changed


def test_sdr_k8s_e2e_still_needs_a_successful_changes_job() -> None:
    """`always()` also runs past a FAILED need; without the explicit `changes`
    result a broken path filter (empty outputs) would decide the job."""
    expression = _gate("pull_request.yaml", "sdr-k8s-e2e")
    for result in ("failure", "cancelled", "skipped"):
        needs = _merge_queue_needs("sdk", changes_result=result)
        assert evaluate(expression, {"github": _merge_group(), "needs": needs}) is False


def test_merge_queue_e2e_still_needs_a_relevant_change() -> None:
    for job in ("connector-tests", "sdr-k8s-e2e"):
        needs = _merge_queue_needs("sdk")
        needs["changes"]["outputs"]["sdk"] = "false"
        contexts = {"github": _merge_group(), "needs": needs}
        assert evaluate(_gate("pull_request.yaml", job), contexts) is False, job


@pytest.mark.parametrize("job", list(_SDK_E2E_JOBS))
def test_forks_and_dependabot_are_still_excluded(job: str) -> None:
    """No secrets on those runs. Pinned with the request present, so the
    exclusion terms alone have to keep the job off; e2e-request also refuses
    them, and either layer failing must not start a live-tenant suite."""
    expression = _gate("pull_request.yaml", job)
    needs = _sdk_needs(job, requested="true", storage="true")
    human = _pull_request("synchronize", ())
    assert evaluate(expression, {"github": human, "needs": needs}) is True
    fork = _pull_request("synchronize", (), fork=True)
    assert evaluate(expression, {"github": fork, "needs": needs}) is False
    bot = _pull_request("synchronize", (), login="dependabot[bot]")
    assert evaluate(expression, {"github": bot, "needs": needs}) is False


def test_the_e2e_concurrency_groups_still_refuse_to_cancel_in_progress() -> None:
    """FND-48 deliberately did not touch this; pin it so the pairing stays visible.

    Making the gate event-aware removes the *spurious* triggers that made the
    queueing painful. Whether a *genuine* re-trigger should cancel an in-flight
    e2e is a separate decision, and it cannot be taken until an abandoned live
    Automation Engine run has a cleanup path — cancelling mid-run today leaves
    tenant state behind. If this assertion is what fails, that decision is being
    made implicitly.

    Tracked as FND-252, which is where a deliberate flip belongs: it carries the
    cleanup-path prerequisite and the group-keying constraint that has to survive
    the change (run-unique off the PR path, or a cross-repo dispatch collapses
    every run into one group — FND-218). This guard has already caught one
    incidental attempt, in the FND-250 lease work, where turning it on had been
    added as a runner-time win rather than as the decision it is.
    """
    workflow = _load(_WORKFLOW_DIR / "tests-reusable.yaml")
    e2e_jobs = [
        name
        for name, job in workflow["jobs"].items()
        if isinstance(job, dict)
        and "e2e" in str(job.get("concurrency", {}).get("group", ""))
    ]
    assert e2e_jobs, "no e2e concurrency group found; the guard has drifted"
    for name in e2e_jobs:
        assert (
            workflow["jobs"][name]["concurrency"]["cancel-in-progress"] is False
        ), name


# ── The done-when criterion of FND-3839 ──────────────────────────────────────


def _pull_request_types(workflow: dict[str, Any]) -> dict[str, list[str]]:
    triggers = _triggers(workflow)
    found: dict[str, list[str]] = {}
    for event in ("pull_request", "pull_request_target"):
        if event in triggers:
            found[event] = list((triggers[event] or {}).get("types") or [])
    return found


def test_no_test_workflow_or_scaffold_subscribes_to_labeled() -> None:
    """A `labeled` / `unlabeled` subscription is how a bot label became a
    duplicate run on the same commit — one whose concurrency group evicted the
    real run and left a failed required check (FND-48, FND-3832). Requests are
    PR comments now; nothing should be listening for labels.

    Covers application-sdk's own workflows and every scaffold as a connector
    receives it. If a workflow genuinely has to react to a label, it needs a
    reason recorded here, not a silent allowlist entry.
    """
    offenders: list[str] = []
    swept = 0
    for path, workflow in _sweep_targets():
        if workflow is None:
            continue
        swept += 1
        for event, types in _pull_request_types(workflow).items():
            hits = sorted({"labeled", "unlabeled"} & set(types))
            if hits:
                offenders.append(f"{path.name}: {event} {hits}")
    assert swept, "the sweep found no workflows; its globs have drifted"
    assert not offenders, f"these still subscribe to label events: {offenders}"


# ── tests-reusable: the post-FND-3839 request paths ──────────────────────────


#: The resynced scaffold's inputs on its own pull_request run.
_SCAFFOLD_INPUTS = {"e2e-label-trigger": False}


def test_the_resynced_scaffold_never_starts_e2e_from_a_label() -> None:
    """`e2e-label-trigger: false` — the scaffold no longer subscribes to
    `labeled`, so a label still on the PR must not start e2e on a push: e2e
    is a `/e2e` dispatch, and the PR's own run would double it."""
    expression = _gate("tests-reusable.yaml", "discover-e2e")
    for label in ("e2e", "gate-check"):
        for description, github, _expected in _scenarios(label):
            github = {**github, "head_ref": "bump-version-main"}
            contexts = {"github": github, "inputs": _SCAFFOLD_INPUTS}
            assert evaluate(expression, contexts) is False, (label, description)


@pytest.mark.parametrize("job", ["detect-integration", "integration"])
def test_the_resynced_scaffold_ignores_labels_for_queued_integration(job: str) -> None:
    """A queued repo skips integration on PRs; a label must not re-enable it
    there, and the no-queue path must not lose it."""
    expression = _gate("tests-reusable.yaml", job)
    for enabled in (True, False):
        extra = (
            _queue_needs(enabled=enabled)
            if job == "detect-integration"
            else _integration_needs(enabled=enabled)
        )
        for description, github, _expected in _scenarios("e2e"):
            contexts = {"github": github, "inputs": _SCAFFOLD_INPUTS, **extra}
            assert evaluate(expression, contexts) is (not enabled), (
                enabled,
                description,
            )


def _dispatch(sha: str = "tip") -> dict[str, Any]:
    return {"event_name": "workflow_dispatch", "sha": sha, "event": {}}


@pytest.mark.parametrize("run_e2e", ["true", "only"])
@pytest.mark.parametrize(
    ("head_sha", "runs"),
    [
        ("", True),
        ("tip", True),
        # A push landed between the comment and the dispatch: the branch tip
        # is not the commit the request was made on.
        ("older", False),
    ],
)
def test_a_dispatch_stands_e2e_down_when_the_branch_moved(
    run_e2e: str, head_sha: str, runs: bool
) -> None:
    """Testing — and recording a verdict on — a commit nobody asked about
    would green the wrong head (FND-3839)."""
    expression = _gate("tests-reusable.yaml", "discover-e2e")
    contexts = {
        "github": _dispatch(),
        "inputs": {"run-e2e": run_e2e, "head-sha": head_sha},
    }
    assert evaluate(expression, contexts) is runs


@pytest.mark.parametrize(
    ("job", "needs"),
    [
        ("unit", {}),
        ("detect-integration", {}),
        (
            "integration",
            {"detect-integration": {"result": "success", "outputs": {"count": "1"}}},
        ),
        ("scorecard", {}),
    ],
)
@pytest.mark.parametrize(("run_e2e", "runs"), [("only", False), ("true", True)])
def test_an_e2e_only_run_skips_the_tiers_its_pr_run_measured(
    job: str, needs: dict[str, Any], run_e2e: str, runs: bool
) -> None:
    """`/e2e` dispatches `run-e2e: only`. Unit and integration already ran on
    the PR's own run, which owns the required `tests / …` contexts; this run
    reports under `e2e / …`. The full-DAG dispatch (`/gate-check`, the SDK
    fan-out) keeps every tier."""
    expression = _gate("tests-reusable.yaml", job)
    contexts = {
        "github": _dispatch(),
        "inputs": {"run-e2e": run_e2e},
        "needs": needs,
    }
    assert evaluate(expression, contexts) is runs


def test_an_e2e_only_run_still_runs_e2e() -> None:
    expression = _gate("tests-reusable.yaml", "discover-e2e")
    contexts = {"github": _dispatch(), "inputs": {"run-e2e": "only"}}
    assert evaluate(expression, contexts) is True


@pytest.mark.parametrize(
    ("run_e2e", "detect", "wakes"),
    [
        # e2e-only skips detection on purpose; e2e still needs the source.
        ("only", "skipped", True),
        # Elsewhere a skipped detection is not a reason to wake anything.
        ("true", "skipped", False),
        ("", "skipped", False),
    ],
)
def test_an_e2e_only_run_still_wakes_the_dataforge_source(
    run_e2e: str, detect: str, wakes: bool
) -> None:
    expression = _gate("tests-reusable.yaml", "wake-dataforge-source")
    contexts = {
        "inputs": {
            "run-e2e": run_e2e,
            "dataforge-lifecycle": True,
            "dataforge-datasource": "ds",
            "dataforge-mode": "resource",
        },
        "needs": {
            "detect-integration": {"result": detect, "outputs": {}},
            "discover-e2e": {"result": "success", "outputs": {"count": "1"}},
        },
    }
    assert evaluate(expression, contexts) is wakes


@pytest.mark.parametrize("job", ["tests-passed", "report-to-sdk"])
def test_both_gate_evaluations_excuse_unit_only_on_an_e2e_only_run(job: str) -> None:
    """The skipped unit job of an e2e-only run would otherwise red the gate;
    one verdict, two callers, so both must pass the same flag."""
    workflow = _load(_WORKFLOW_DIR / "tests-reusable.yaml")
    steps = [
        step
        for step in workflow["jobs"][job]["steps"]
        if "verify-test-gate" in str(step.get("uses", ""))
    ]
    assert len(steps) == 1, f"{job} should evaluate the gate exactly once"
    flag = steps[0]["with"]["e2e-only"]
    assert flag == "${{ inputs.run-e2e == 'only' }}"


# ── The tests.yaml scaffold's side of the contract ───────────────────────────


def _tests_caller() -> dict[str, Any]:
    workflow = _rendered_template(_TEMPLATE_DIR / "tests.yaml")
    assert workflow is not None
    return workflow["jobs"]["tests"]


@pytest.mark.parametrize(
    ("run_e2e", "name"), [("only", "e2e"), ("true", "tests"), ("", "tests")]
)
def test_an_e2e_only_run_reports_under_e2e_not_tests(run_e2e: str, name: str) -> None:
    """GitHub resolves a required check to the NEWEST run on the commit and
    counts a skipped job as a pass. An e2e-only run publishing a skipped
    `tests / Unit` would therefore override a real unit failure from the PR's
    own run, so it must report under another prefix."""
    caller = _tests_caller()
    expression = caller["name"]
    assert expression.startswith("${{") and expression.endswith("}}")
    rendered = evaluate_operand(
        expression[3:-2].strip(), {"inputs": {"run_e2e": run_e2e}}
    )
    assert rendered == name


def test_the_scaffold_turns_the_label_trigger_off_and_forwards_the_head() -> None:
    caller = _tests_caller()
    assert caller["with"]["e2e-label-trigger"] is False
    assert caller["with"]["head-sha"] == "${{ inputs.head_sha }}"
    assert caller["with"]["run-e2e"] == "${{ inputs.run_e2e }}"
    inputs = _triggers(_rendered_template(_TEMPLATE_DIR / "tests.yaml") or {})[
        "workflow_dispatch"
    ]["inputs"]
    assert "head_sha" in inputs and "run_e2e" in inputs


def test_the_e2e_only_marker_matches_what_test_commands_checks_for() -> None:
    """pr_test_command refuses `/e2e` on a branch whose tests.yaml lacks the
    `e2e` caller name; if the template's spelling drifts from the marker it
    greps for, every `/e2e` is refused as "rebase onto main"."""
    text = (_TEMPLATE_DIR / "tests.yaml").read_text(encoding="utf-8")
    assert pr_test_command.E2E_ONLY_MARKER in text

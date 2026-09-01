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

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github/workflows"

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
    and the SDK's own bootstrap template ships `labeled` in every connector's
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


def test_every_label_gate_reachable_by_a_labeled_event_is_event_aware() -> None:
    offenders: list[str] = []
    checked = 0
    for path in sorted(_WORKFLOW_DIR.glob("*.y*ml")):
        workflow = _load_or_skip(path)
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


def test_build_sdk_base_image_only_reacts_to_the_e2e_label_being_added() -> None:
    _assert_gate(
        _gate("pull_request.yaml", "build-sdk-base-image"),
        "e2e",
        {"needs": _CHANGES_SDK},
    )


def test_connector_tests_only_reacts_to_the_e2e_label_being_added() -> None:
    _assert_gate(
        _gate("pull_request.yaml", "connector-tests"),
        "e2e",
        {
            "needs": {
                **_CHANGES_SDK,
                "matrix-builder": {"result": "success"},
                "build-sdk-base-image": {"result": "success"},
                "merge-sdk-base-image": {"result": "success"},
            }
        },
    )


def test_sdr_k8s_e2e_only_reacts_to_the_e2e_label_being_added() -> None:
    _assert_gate(
        _gate("pull_request.yaml", "sdr-k8s-e2e"), "e2e", {"needs": _CHANGES_SDK}
    )


def test_storage_integration_only_reacts_to_the_e2e_label_being_added() -> None:
    """FND-1153: the real-cloud storage suite rides the 'e2e' release tier.

    It briefly moved onto the merge-queue-blocking SDK Gate and had to come
    back: the Entra federated credential has no subject for a
    `gh-readonly-queue/*` ref, so every queue entry red-lined on AADSTS700213
    before a test ran. Asserting the label here pins the tier — a silent flip
    back to a private 'storage-integration' label would make the suite opt-in
    via a label nothing applies, which is how it went ~never-run before.
    """
    _assert_gate(_gate("pull_request.yaml", "storage-integration"), "e2e", {})


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


@pytest.mark.parametrize("job", ["connector-tests", "sdr-k8s-e2e"])
def test_the_merge_queue_path_is_unaffected_by_the_label_term(job: str) -> None:
    """merge_group has no labels and no `labeled` action; it must still run."""
    contexts = {
        "github": _merge_group(),
        "needs": {
            **_CHANGES_SDK,
            "matrix-builder": {"result": "success"},
            # Skipped on the merge-queue path — the gate tolerates that by design.
            "build-sdk-base-image": {"result": "skipped"},
            "merge-sdk-base-image": {"result": "skipped"},
        },
    }
    assert evaluate(_gate("pull_request.yaml", job), contexts) is True


@pytest.mark.parametrize(
    ("job", "extra"),
    [
        ("build-sdk-base-image", {"needs": _CHANGES_SDK}),
        ("sdr-k8s-e2e", {"needs": _CHANGES_SDK}),
    ],
)
def test_forks_and_dependabot_are_still_excluded(
    job: str, extra: dict[str, Any]
) -> None:
    """The fix adds a term; it must not have loosened the existing ones."""
    expression = _gate("pull_request.yaml", job)
    fork = _pull_request("labeled", ("e2e",), added="e2e", fork=True)
    assert evaluate(expression, {"github": fork, **extra}) is False
    bot = _pull_request("labeled", ("e2e",), added="e2e", login="dependabot[bot]")
    assert evaluate(expression, {"github": bot, **extra}) is False


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

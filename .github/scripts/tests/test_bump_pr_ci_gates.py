"""Bump-version PR CI is opt-in via a `/gate-check` comment (FND-3748, FND-3839).

The opt-in used to be a `gate-check` label, which needed every gated workflow to
subscribe to `labeled` — and so created a duplicate run on the same commit for
every label any bot added. It is now a PR comment, handled two ways:

* connector repos: `/gate-check` DISPATCHES the gated shims on the bump branch
  (pr_test_command.py). The PR's own run passes `skip-on-bump-pr: true`, which
  skips every job inside the reusable, so each required `<caller> / <job>`
  check files as `skipped` (FND-3947). A dispatch is not a `pull_request`, so
  it runs everything;
* application-sdk: `/gate-check` posts a `gate-check` commit status and RE-RUNS
  the runs on the commit, which read that status live (bump_pr_opt_in.py).

Evaluates the real `if:` expressions lifted out of the workflow YAML and the
bootstrap templates against synthetic payloads, so a gate wired one paren out
(`&&` binds tighter than `||` in GHA) fails here rather than in the fleet:

* app shims always run their caller job and skip every job INSIDE the
  reusable on a bump PR's own run: free, and every required context still
  resolves. They run on the `/gate-check` dispatch and ignore a `gate-check`
  label;
* `/gate-check` implies e2e on a bump PR (apps and SDK `bump-version-main`), but
  never triggers tenant e2e on the conformance / contract-toolkit lanes;
* SDK Gate, an inline required gate, fails closed until opted in rather than
  skipping (a skipped required check reads as a pass). It is the one red check
  an un-opted bump PR carries: Connector Tests Gate skips, which is safe only
  because SDK Gate blocks the merge (FND-4239). Every SDK workflow
  `/gate-check` re-runs reads the opt-in from the commit;
* legacy app repos still have their label cleared before each bump push.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bump_pr_opt_in  # noqa: E402
import pr_test_command  # noqa: E402
from _bump_skip import BUMP_SKIP_TERM  # noqa: E402
from _gha_expr import evaluate, evaluate_operand  # noqa: E402
from conformance.bootstrap import render as bootstrap_render  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github/workflows"
TEMPLATES = REPO_ROOT / "packages/conformance/conformance/bootstrap/templates"


def _load(path: Path) -> dict[str, Any]:
    if path.parent == TEMPLATES:
        # Bootstrap's own renderer with default slots — what a fresh connector
        # repo receives — fed this checkout's template text. The installed
        # conformance package is a non-editable copy that can lag the source.
        with mock.patch.object(
            bootstrap_render,
            "_load_template",
            lambda name: (TEMPLATES / name).read_text(encoding="utf-8"),
        ):
            return yaml.safe_load(bootstrap_render.render(path.name))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _strip(expression: str) -> str:
    source = str(expression).strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2].strip()
    return source


def _job_if(path: Path, job: str) -> str:
    return _strip(_load(path)["jobs"][job]["if"])


def _step(path: Path, job: str, name: str) -> dict[str, Any]:
    steps = _load(path)["jobs"][job]["steps"]
    matches = [s for s in steps if s.get("name") == name]
    assert matches, f"{path.name}:{job} lost its {name!r} step"
    return matches[0]


def _ctx(
    head_ref: str,
    *,
    action: str = "synchronize",
    labels: tuple[str, ...] = (),
    added: str | None = None,
    event: str = "pull_request",
    ref_name: str = "",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "labels": [{"name": n} for n in labels],
            "head": {"repo": {"fork": False}},
            "user": {"login": "a-human"},
        },
    }
    if added is not None:
        payload["label"] = {"name": added}
    contexts: dict[str, Any] = {
        "github": {
            "event_name": event,
            "head_ref": head_ref,
            "ref_name": ref_name,
            "actor": "someone",
            "event": payload,
        }
    }
    contexts.update(extra)
    return contexts


def _interpolate(template: str, contexts: dict[str, Any]) -> str:
    """Render a `prefix-${{ expr }}-…` string the way GitHub does."""
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda m: str(evaluate_operand(m.group(1).strip(), contexts)),
        template,
    )


def _dispatch(branch: str) -> dict[str, Any]:
    """A `/gate-check` dispatch: no PR payload, and `head_ref` is empty."""
    return {
        "github": {
            "event_name": "workflow_dispatch",
            "head_ref": "",
            "ref_name": branch,
            "ref": f"refs/heads/{branch}",
            "actor": "someone",
            "event": {},
        }
    }


BUMP = "bump-version-main"
OPTED = ("release", "gate-check")

#: tests-reusable.yaml's inputs as a caller predating FND-3839 passes them (it
#: still opts in with the label), and as the resynced scaffold does.
_LEGACY_INPUTS = {
    "enable-e2e": True,
    "run-e2e": "",
    "head-sha": "",
    "e2e-label-trigger": True,
}
_SCAFFOLD_INPUTS = {**_LEGACY_INPUTS, "e2e-label-trigger": False}

#: (description, context, whether the reusable's jobs run) for a gated shim.
_CALLER_SCENARIOS = [
    ("ordinary PR push", _ctx("feat/x"), True),
    ("ordinary PR opened", _ctx("feat/x", action="opened"), True),
    ("merge queue", _ctx("", event="merge_group", action="checks_requested"), True),
    ("push to main", _ctx("", event="push", action=""), True),
    ("bump PR push", _ctx(BUMP, labels=("release",)), False),
    ("bump PR opened", _ctx(BUMP, action="opened", labels=("release",)), False),
    # A label no longer opts in: the bump PR's own run never runs the gate,
    # whatever it carries. Only the dispatch below does.
    ("bump PR carrying a stale gate-check label", _ctx(BUMP, labels=OPTED), False),
    ("bump PR carrying a legacy e2e label", _ctx(BUMP, labels=("e2e",)), False),
    ("/gate-check dispatch on the bump branch", _dispatch(BUMP), True),
    ("/gate-check dispatch on a subpackage lane", _dispatch("bump-version-x"), True),
]

#: The connector shims a `/gate-check` dispatches: (template, caller job,
#: reusable it calls).
_GATED_SHIMS = [
    ("tests.yaml", "tests", "tests-reusable.yaml"),
    ("checks.yml", "pre-commit", "checks-reusable.yaml"),
    ("conformance.yaml", "suite", "conformance-reusable.yaml"),
    ("vulnerability-scan.yml", "scan", "build-and-scan.yaml"),
]
_GATED_REUSABLES = [reusable for _, _, reusable in _GATED_SHIMS]


def _with_input(ctx: dict[str, Any], skip: bool | None) -> dict[str, Any]:
    inputs = {} if skip is None else {"skip-on-bump-pr": skip}
    return {**ctx, "inputs": inputs}


@pytest.mark.parametrize(("template", "job", "_reusable"), _GATED_SHIMS)
@pytest.mark.parametrize(("desc", "ctx", "_runs"), _CALLER_SCENARIOS)
def test_shims_run_their_caller_job_on_every_event(
    template: str,
    job: str,
    _reusable: str,
    desc: str,
    ctx: dict[str, Any],
    _runs: bool,
) -> None:
    """A skipped CALLER job files only `<caller>`, never the required
    `<caller> / <job>` names, so a bump PR sat at "Expected" forever: GitHub
    does not count the `/gate-check` dispatch toward a PR's required checks."""
    caller = _load(TEMPLATES / template)["jobs"][job]
    condition = caller.get("if")
    assert condition is None or "head_ref" not in str(condition), template
    if condition is not None:
        ctx = {**ctx, "needs": {"candidate": {"result": "skipped"}}}
        assert evaluate(_strip(condition), ctx) is True, desc


@pytest.mark.parametrize(("template", "job", "_reusable"), _GATED_SHIMS)
def test_shims_ask_their_reusable_to_skip_the_bump_pr_run(
    template: str, job: str, _reusable: str
) -> None:
    assert _load(TEMPLATES / template)["jobs"][job]["with"]["skip-on-bump-pr"] is True


@pytest.mark.parametrize("reusable", _GATED_REUSABLES)
def test_every_job_in_a_gated_reusable_starts_with_the_bump_skip(
    reusable: str,
) -> None:
    """Every job, including the `always()` gates: one left out would run after
    its needs were skipped, and either bill a runner or fail the check."""
    jobs = _load(WORKFLOWS / reusable)["jobs"]
    missing = [
        name
        for name, job in jobs.items()
        if not _strip(job.get("if", "")).startswith(BUMP_SKIP_TERM + " &&")
    ]
    assert missing == []


@pytest.mark.parametrize("reusable", _GATED_REUSABLES)
def test_the_skip_is_off_unless_the_caller_asks(reusable: str) -> None:
    """The SDK's sdk-gate calls conformance-reusable on its own bump PRs, and a
    pre-FND-3839 shim runs tests-reusable on a labelled bump PR whose Release
    Gate passes on the label. Neither may see a skip it did not ask for."""
    spec = _load(WORKFLOWS / reusable)[True]["workflow_call"]["inputs"][
        "skip-on-bump-pr"
    ]
    assert spec["type"] == "boolean" and spec["default"] is False


@pytest.mark.parametrize("reusable", _GATED_REUSABLES)
@pytest.mark.parametrize(("desc", "ctx", "runs"), _CALLER_SCENARIOS)
@pytest.mark.parametrize("skip", [True, False, None])
def test_the_bump_skip_term_fires_only_on_an_opted_bump_pr_run(
    reusable: str, desc: str, ctx: dict[str, Any], runs: bool, skip: bool | None
) -> None:
    expected = runs or skip is not True
    for name, job in _load(WORKFLOWS / reusable)["jobs"].items():
        term = _strip(job["if"])[: len(BUMP_SKIP_TERM)]
        assert evaluate(term, _with_input(ctx, skip)) is expected, (name, desc)


@pytest.mark.parametrize("reusable", _GATED_REUSABLES)
def test_an_opted_bump_pr_run_skips_every_job_whatever_its_needs(
    reusable: str,
) -> None:
    """`&&` short-circuits, so the rest of the condition (needs results,
    `always()`) cannot rescue a job on the bump PR's own run."""
    ctx = _with_input(_ctx(BUMP, labels=OPTED), True)
    for name, job in _load(WORKFLOWS / reusable)["jobs"].items():
        assert evaluate(_strip(job["if"]), ctx) is False, name


@pytest.mark.parametrize(("template", "_job", "_reusable"), _GATED_SHIMS)
def test_gated_shims_can_be_dispatched_and_never_subscribe_to_labels(
    template: str, _job: str, _reusable: str
) -> None:
    """Without `workflow_dispatch` the `/gate-check` comment has nothing to
    start; with `labeled` every bot label would start a duplicate run."""
    on = _load(TEMPLATES / template)[True]
    assert "workflow_dispatch" in on
    types = (on.get("pull_request") or {}).get("types") or []
    assert "labeled" not in types and "unlabeled" not in types


def test_gate_check_dispatches_exactly_the_gated_shims() -> None:
    """The command's list and this file's must not drift: a shim the command
    skips never runs its gate, and one it dispatches that skips the dispatch
    burns a run for nothing."""
    assert set(pr_test_command.GATE_CHECK_WORKFLOWS) == {t for t, _, _ in _GATED_SHIMS}


def test_the_verdict_listens_for_every_gated_shim_by_name() -> None:
    """`workflow_run` matches on each workflow's `name:`, not its file. A name
    missing here leaves `gate-check` pending forever on a bump PR that has it."""
    listened = set(
        _load(TEMPLATES / "test-commands.yaml")[True]["workflow_run"]["workflows"]
    )
    names = {_load(TEMPLATES / template)["name"] for template, _, _ in _GATED_SHIMS}
    assert names == listened


@pytest.mark.parametrize(
    ("template", "group"),
    [
        ("checks.yml", lambda w: w["jobs"]["pre-commit"]["concurrency"]["group"]),
        ("vulnerability-scan.yml", lambda w: w["concurrency"]["group"]),
    ],
)
def test_a_gate_check_dispatch_never_shares_the_pr_runs_cancel_group(
    template: str, group: Any
) -> None:
    """The dispatch and the PR's own run must not evict one another (FND-218)."""
    expression = group(_load(TEMPLATES / template))
    pr = _ctx(BUMP)
    pr["github"].update(ref="refs/pull/7/merge", workflow="W")
    pr["github"]["event"]["pull_request"]["number"] = 7
    dispatch = _dispatch(BUMP)
    dispatch["github"]["workflow"] = "W"
    assert _interpolate(expression, pr) != _interpolate(expression, dispatch)


# ── tests-reusable: gate-check implies e2e on a bump PR only ─────────────────


@pytest.mark.parametrize(
    ("head_ref", "labels", "added", "runs"),
    [
        (BUMP, OPTED, "gate-check", True),
        ("bump-version-conformance", OPTED, "gate-check", True),
        ("feat/x", ("gate-check",), "gate-check", False),
        (BUMP, (*OPTED, "size/S"), "size/S", False),
        ("feat/x", ("e2e",), "e2e", True),
    ],
)
def test_a_legacy_caller_still_accepts_the_gate_check_label_on_a_bump_pr(
    head_ref: str, labels: tuple[str, ...], added: str, runs: bool
) -> None:
    """Callers that predate FND-3839 opt in with the label until they resync."""
    ctx = _ctx(head_ref, action="labeled", labels=labels, added=added)
    ctx["inputs"] = _LEGACY_INPUTS
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", "discover-e2e")
    assert evaluate(expr, ctx) is runs


def test_the_resynced_scaffold_ignores_the_gate_check_label() -> None:
    ctx = _ctx(BUMP, action="labeled", labels=OPTED, added="gate-check")
    ctx["inputs"] = _SCAFFOLD_INPUTS
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", "discover-e2e")
    assert evaluate(expr, ctx) is False


@pytest.mark.parametrize(
    ("head_sha", "runs"),
    [
        ("", True),
        ("tip", True),
        # A push landed between the comment and the dispatch: the tip is not
        # the commit the reviewer opted in.
        ("older", False),
    ],
)
def test_a_gate_check_dispatch_runs_e2e_on_the_commented_commit(
    head_sha: str, runs: bool
) -> None:
    """`/gate-check` dispatches tests.yaml with `run_e2e: true` (full DAG)."""
    ctx = _dispatch(BUMP)
    ctx["github"]["sha"] = "tip"
    ctx["inputs"] = {**_SCAFFOLD_INPUTS, "run-e2e": "true", "head-sha": head_sha}
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", "discover-e2e")
    assert evaluate(expr, ctx) is runs


@pytest.mark.parametrize("job", ["detect-integration", "integration"])
def test_queued_repo_integration_on_a_bump_pr(job: str) -> None:
    """A queued repo skips integration on PRs; the release gate must not."""
    needs = {
        "detect-merge-queue": {"outputs": {"enabled": "true"}},
        "detect-integration": {"result": "success", "outputs": {"count": "2"}},
    }
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", job).replace("always() &&", "", 1)
    opted = _ctx(
        BUMP,
        action="labeled",
        labels=OPTED,
        added="gate-check",
        needs=needs,
        inputs=_LEGACY_INPUTS,
    )
    plain = _ctx(
        "feat/x",
        action="labeled",
        labels=("gate-check",),
        added="gate-check",
        needs=needs,
        inputs=_LEGACY_INPUTS,
    )
    assert evaluate(expr, opted) is True
    assert evaluate(expr, plain) is False
    # The resynced path: the `/gate-check` dispatch is not a pull_request, so
    # integration runs regardless of the queue.
    dispatch = {**_dispatch(BUMP), "needs": needs}
    dispatch["inputs"] = {**_SCAFFOLD_INPUTS, "run-e2e": "true"}
    assert evaluate(expr, dispatch) is True


# ── SDK repo ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("head_ref", "requested"),
    [
        (BUMP, True),
        ("bump-version-conformance", False),
        ("bump-version-contract-toolkit", False),
        ("feat/x", False),
    ],
)
def test_sdk_tenant_e2e_follows_gate_check_on_the_sdk_lane_only(
    monkeypatch: pytest.MonkeyPatch, head_ref: str, requested: bool
) -> None:
    """Conformance / toolkit releases never pay for tenant e2e (FND-3748).

    Two halves: e2e-request decides from the commit's statuses (here: opted in
    with `/gate-check`, no `/e2e`), and every tenant-e2e job follows its answer.
    """
    states = {bump_pr_opt_in.GATE_CHECK_CONTEXT: "success"}
    monkeypatch.setattr(
        bump_pr_opt_in, "commit_status", lambda _r, _s, context: states.get(context, "")
    )
    answer, _ = bump_pr_opt_in.is_e2e_requested(
        "o/r", head_ref, "sha", fork=False, author="a-human"
    )
    assert answer is requested

    needs = {
        "changes": {
            "result": "success",
            "outputs": {
                "sdk": "false",
                "container": "false",
                "ci": "false",
                "storage": "false",
            },
        },
        "e2e-request": {"outputs": {"requested": str(answer).lower()}},
    }
    ctx = _ctx(head_ref, needs=needs)
    for job in ("sdr-k8s-e2e", "storage-integration"):
        assert evaluate(_job_if(WORKFLOWS / "pull_request.yaml", job), ctx) is (
            requested
        ), job


def test_sdk_e2e_request_reads_the_head_commit() -> None:
    """Live from the commit: `/e2e` and `/gate-check` re-run this job on a run
    that started before the marker, and a re-run replays the old payload."""
    path = WORKFLOWS / "pull_request.yaml"
    job = _load(path)["jobs"]["e2e-request"]
    assert evaluate(job["if"], _ctx("feat/x")) is True
    assert evaluate(job["if"], _ctx("", event="merge_group")) is False
    assert job["permissions"]["statuses"] == "read"
    step = _step(path, "e2e-request", "Read the e2e request on the head commit")
    assert 'bump_pr_opt_in.py --repo "$REPO" e2e-requested' in step["run"]
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert step["env"]["HEAD_REF"] == "${{ github.head_ref }}"


@pytest.mark.parametrize(
    "job",
    [
        "sdk-tests",
        "conformance",
        "toolkit",
        "conformance-suite-tests",
        "api-suite-tests",
    ],
)
@pytest.mark.parametrize(("opted_in", "runs"), [("true", True), ("false", False)])
def test_sdk_gate_areas_wait_for_the_opt_in(
    job: str, opted_in: str, runs: bool
) -> None:
    outputs = {
        "opted_in": opted_in,
        "sdk": "true",
        "toolkit": "true",
        "conformance": "true",
        "conformance_pkg": "true",
        "api_pkg": "true",
        "container": "false",
        "force_sdk": "true",
        "force_conformance": "true",
        "force_toolkit": "true",
    }
    expr = _job_if(WORKFLOWS / "sdk-gate.yaml", job)
    assert evaluate(expr, {"needs": {"changes": {"outputs": outputs}}}) is runs


@pytest.mark.parametrize(
    ("forced", "runs"),
    [
        ({"force_sdk": "true"}, True),
        ({"force_all": "true", "force_sdk": "true"}, True),
        ({"force_conformance": "true"}, False),
        ({"force_toolkit": "true"}, False),
        ({}, False),
    ],
)
@pytest.mark.parametrize("job", ["sdk-tests", "api-suite-tests"])
def test_sdk_gate_force_sdk_runs_the_sdk_and_api_suites(
    job: str, forced: dict[str, str], runs: bool
) -> None:
    """A bump-version-main PR sets force_sdk and skips paths-filter, so the
    area outputs are empty strings: force_sdk alone must still run both the
    SDK suite and the api package's suite (the api wheel ships with it)."""
    outputs = {
        "opted_in": "true",
        "sdk": "",
        "api_pkg": "",
        "container": "",
        "force_all": "false",
        "force_sdk": "false",
        "force_conformance": "false",
        "force_toolkit": "false",
        **forced,
    }
    expr = _job_if(WORKFLOWS / "sdk-gate.yaml", job)
    assert evaluate(expr, {"needs": {"changes": {"outputs": outputs}}}) is runs


@pytest.mark.parametrize(
    ("opted_in", "fails"), [("false", True), ("true", False), ("", True)]
)
def test_sdk_gate_fails_closed_until_opted_in(opted_in: str, fails: bool) -> None:
    """Fails rather than skips: a skipped required check reads as a pass.

    The only check that keeps an un-opted bump PR out of the merge queue:
    Connector Tests Gate skips on one (FND-4239), so this must not.
    """
    path, job = WORKFLOWS / "sdk-gate.yaml", "gate"
    assert _strip(_load(path)["jobs"][job]["if"]) == "always()"
    found = _step(path, job, "Require the bump-PR CI opt-in")
    ctx = {
        "needs": {"changes": {"result": "success", "outputs": {"opted_in": opted_in}}}
    }
    assert evaluate(_strip(found["if"]), ctx) is fails
    assert "exit 1" in found["run"]
    assert "/gate-check" in found["run"]
    steps = _load(path)["jobs"][job]["steps"]
    assert steps.index(found) == 0, "must fail before anything reads skips as passes"


@pytest.mark.parametrize(
    ("changes", "requested", "runs"),
    [
        # Un-opted bump PR: nothing upstream ran, so the gate skips rather
        # than failing every push red (FND-4239). SDK Gate blocks the merge.
        ({"result": "success", "outputs": {"opted_in": "false"}}, "false", False),
        ({"result": "success", "outputs": {"opted_in": "false"}}, None, False),
        # Opted in, any ordinary PR, and the merge queue (empty head_ref).
        ({"result": "success", "outputs": {"opted_in": "true"}}, "false", True),
        ({"result": "success", "outputs": {"opted_in": "true"}}, None, True),
        # `/e2e` on an un-opted bump PR dispatched e2e: wait on it and report.
        ({"result": "success", "outputs": {"opted_in": "false"}}, "true", True),
        # Detection did not succeed: run, and fail on it, never skip.
        ({"result": "failure", "outputs": {}}, "false", True),
        ({"result": "cancelled", "outputs": {}}, None, True),
    ],
)
def test_connector_gate_skips_only_an_un_opted_bump_pr(
    changes: dict[str, Any], requested: str | None, runs: bool
) -> None:
    e2e_request = {"outputs": {} if requested is None else {"requested": requested}}
    ctx = {"needs": {"changes": changes, "e2e-request": e2e_request}}
    path = WORKFLOWS / "pull_request.yaml"
    assert evaluate(_job_if(path, "connector-gate"), ctx) is runs
    names = [s.get("name") for s in _load(path)["jobs"]["connector-gate"]["steps"]]
    assert "Require the bump-PR CI opt-in" not in names


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("sdk-gate.yaml", "changes"),
        ("pull_request.yaml", "changes"),
        ("codeql.yaml", "opt-in"),
    ],
)
def test_sdk_opt_in_reads_the_head_commit(workflow: str, job: str) -> None:
    """A re-run replays the original payload, so only the commit's live
    `gate-check` status can carry an opt-in made after the run started."""
    step = _step(WORKFLOWS / workflow, job, "Check the bump-PR CI opt-in")
    assert "bump_pr_opt_in.py check" in step["run"]
    assert '--sha "$HEAD_SHA"' in step["run"]
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_gate_check_reruns_every_sdk_workflow_that_reads_the_opt_in() -> None:
    """The comment re-runs a fixed list; a workflow that reads the opt-in but
    is missing from it stays un-opted forever (its required check red), and
    one that is listed but never reads it re-runs for nothing."""
    readers = {
        path.name
        for path in WORKFLOWS.glob("*.y*ml")
        if "bump_pr_opt_in.py check" in path.read_text(encoding="utf-8")
    }
    assert readers == set(pr_test_command.SDK_GATE_CHECK_WORKFLOWS)


@pytest.mark.parametrize(
    "workflow", ["sdk-gate.yaml", "pull_request.yaml", "codeql.yaml"]
)
def test_sdk_opt_in_workflows_do_not_subscribe_to_labeled(workflow: str) -> None:
    """A label event would add a run whose skips read as passes (sdk-gate's
    required check is inline) — and it opts nothing in any more."""
    on = _load(WORKFLOWS / workflow)[True]
    assert "labeled" not in (on["pull_request"] or {}).get("types", [])


@pytest.mark.parametrize("job", ["analyze", "lint"])
@pytest.mark.parametrize(
    ("ctx", "runs"),
    [
        # Off a bump PR the opt-in job is skipped and its output empty.
        (_ctx("feat/x", needs={"opt-in": {"outputs": {}}}), True),
        (
            _ctx(
                BUMP,
                labels=("release",),
                needs={"opt-in": {"outputs": {"opted_in": "false"}}},
            ),
            False,
        ),
        # A stale label means nothing; the status read by opt-in decides.
        (
            _ctx(
                BUMP, labels=OPTED, needs={"opt-in": {"outputs": {"opted_in": "false"}}}
            ),
            False,
        ),
        (_ctx(BUMP, needs={"opt-in": {"outputs": {"opted_in": "true"}}}), True),
        # The opt-in job failed: no output, so the bump PR stays gated.
        (_ctx(BUMP, needs={"opt-in": {"outputs": {}}}), False),
    ],
)
def test_codeql_waits_for_the_opt_in_on_bump_prs(
    job: str, ctx: dict[str, Any], runs: bool
) -> None:
    assert evaluate(_job_if(WORKFLOWS / "codeql.yaml", job), ctx) is runs


@pytest.mark.parametrize(
    ("ctx", "runs"),
    [
        (_ctx(BUMP), True),
        (_ctx("bump-version-conformance"), True),
        # Everywhere else it costs nothing: skipped, and analyze/lint run.
        (_ctx("feat/x"), False),
        (_ctx("", event="merge_group"), False),
        (_ctx("", event="push"), False),
    ],
)
def test_codeql_opt_in_job_runs_only_on_bump_prs(
    ctx: dict[str, Any], runs: bool
) -> None:
    assert evaluate(_job_if(WORKFLOWS / "codeql.yaml", "opt-in"), ctx) is runs


@pytest.mark.parametrize(
    ("head_ref", "runs"), [("feat/x", True), (BUMP, False), ("", True)]
)
def test_script_tests_never_run_on_bump_prs(head_ref: str, runs: bool) -> None:
    assert (
        evaluate(_job_if(WORKFLOWS / "scripts-tests.yaml", "pytest"), _ctx(head_ref))
        is runs
    )


# ── Legacy app repos: the label is still cleared before every bump push ──────
#
# A connector repo whose shims predate FND-3839 still reads the `gate-check`
# LABEL, and the `synchronize` a push fires carries the PR's labels at that
# instant, so a stale opt-in would start full CI on the new commit. A resynced
# repo opts in with a commit status, which a push leaves behind by
# construction; there the clear is a no-op. The SDK's own release workflows
# never read the label any more, so they no longer clear it.


def test_release_bump_clears_the_opt_in_before_pushing() -> None:
    steps = _load(WORKFLOWS / "release-version-bump.yaml")["jobs"]["bump-version"][
        "steps"
    ]
    names = [s.get("name") for s in steps]
    reset = names.index("Reset the bump PR's CI opt-in")
    push = names.index("Commit and push bump branch")
    assert reset < push
    assert "bump_pr_opt_in.py clear" in steps[reset]["run"]
    # Runs exactly when the push does.
    assert steps[push]["if"] in steps[reset]["if"]

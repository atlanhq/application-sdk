"""Bump-version PR CI is opt-in via the `gate-check` label (FND-3748).

Evaluates the real `if:` expressions lifted out of the workflow YAML and the
bootstrap templates against synthetic payloads, so a gate wired one paren out
(`&&` binds tighter than `||` in GHA) fails here rather than in the fleet:

* app shims skip their CALLER job on an un-opted bump PR — free, and no
  required context is created — and run on the event that adds `gate-check`;
* shims that newly subscribe to `labeled` still start nothing for a label on
  an ordinary PR;
* `gate-check` implies e2e on a bump PR (apps and SDK `bump-version-main`), but
  never triggers tenant e2e on the conformance / contract-toolkit lanes;
* the SDK's inline required gates fail closed until opted in, rather than
  skipping (a skipped required check reads as a pass);
* every bump workflow clears the opt-in BEFORE it pushes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402
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
            "actor": "someone",
            "event": payload,
        }
    }
    contexts.update(extra)
    return contexts


BUMP = "bump-version-main"
OPTED = ("release", "gate-check")

#: (description, context, runs) for a shim CALLER job. `tests.yaml` keeps
#: today's label behaviour on an ordinary PR; the others start nothing there.
_CALLER_SCENARIOS = [
    ("ordinary PR push", _ctx("feat/x"), True),
    ("ordinary PR opened", _ctx("feat/x", action="opened"), True),
    ("merge queue", _ctx("", event="merge_group", action="checks_requested"), True),
    ("push to main", _ctx("", event="push", action=""), True),
    ("un-opted bump push", _ctx(BUMP, labels=("release",)), False),
    ("un-opted bump opened", _ctx(BUMP, action="opened", labels=("release",)), False),
    (
        "gate-check added",
        _ctx(BUMP, action="labeled", labels=OPTED, added="gate-check"),
        True,
    ),
    (
        "unrelated label on an opted-in bump PR",
        _ctx(BUMP, action="labeled", labels=(*OPTED, "size/S"), added="size/S"),
        False,
    ),
    (
        "legacy e2e opt-in",
        _ctx(BUMP, action="labeled", labels=("release", "e2e"), added="e2e"),
        True,
    ),
    (
        "release label added by automation",
        _ctx(BUMP, action="labeled", labels=("release",), added="release"),
        False,
    ),
]


@pytest.mark.parametrize(
    ("template", "job"),
    [("checks.yml", "pre-commit"), ("conformance.yaml", "suite")],
)
@pytest.mark.parametrize(("desc", "ctx", "runs"), _CALLER_SCENARIOS)
def test_newly_labeled_shims_gate_their_caller(
    template: str, job: str, desc: str, ctx: dict[str, Any], runs: bool
) -> None:
    assert evaluate(_job_if(TEMPLATES / template, job), ctx) is runs, desc


@pytest.mark.parametrize(
    ("template", "job"),
    [("checks.yml", "pre-commit"), ("conformance.yaml", "suite")],
)
def test_newly_labeled_shims_skip_label_events_on_ordinary_prs(
    template: str, job: str
) -> None:
    ctx = _ctx("feat/x", action="labeled", labels=("size/S",), added="size/S")
    assert evaluate(_job_if(TEMPLATES / template, job), ctx) is False


@pytest.mark.parametrize(
    "template", ["checks.yml", "conformance.yaml", "vulnerability-scan.yml"]
)
def test_newly_labeled_shims_subscribe_to_labeled(template: str) -> None:
    on = _load(TEMPLATES / template)[True]
    assert "labeled" in on["pull_request"]["types"]


@pytest.mark.parametrize(("desc", "ctx", "runs"), _CALLER_SCENARIOS)
def test_tests_shim_gates_its_caller(
    desc: str, ctx: dict[str, Any], runs: bool
) -> None:
    assert evaluate(_job_if(TEMPLATES / "tests.yaml", "tests"), ctx) is runs, desc


def test_tests_shim_keeps_label_events_on_ordinary_prs() -> None:
    """The reusable filters these (FND-48); the caller must not change that."""
    ctx = _ctx("feat/x", action="labeled", labels=("e2e",), added="e2e")
    assert evaluate(_job_if(TEMPLATES / "tests.yaml", "tests"), ctx) is True


def test_label_runs_never_share_the_heads_cancel_group() -> None:
    """A label event must not cancel the head's in-flight run (FND-218)."""
    checks = _load(TEMPLATES / "checks.yml")["jobs"]["pre-commit"]["concurrency"]
    assert "github.event.action == 'labeled' && github.run_id" in checks["group"]
    scan = _load(TEMPLATES / "vulnerability-scan.yml")["concurrency"]
    assert "github.event.action == 'labeled' && github.run_id" in scan["group"]


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
def test_discover_e2e_accepts_gate_check_on_a_bump_pr(
    head_ref: str, labels: tuple[str, ...], added: str, runs: bool
) -> None:
    ctx = _ctx(head_ref, action="labeled", labels=labels, added=added)
    ctx["inputs"] = {"enable-e2e": True, "run-e2e": ""}
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", "discover-e2e")
    assert evaluate(expr, ctx) is runs


@pytest.mark.parametrize("job", ["detect-integration", "integration"])
def test_queued_repo_integration_accepts_gate_check_on_a_bump_pr(job: str) -> None:
    needs = {
        "detect-merge-queue": {"outputs": {"enabled": "true"}},
        "detect-integration": {"result": "success", "outputs": {"count": "2"}},
    }
    expr = _job_if(WORKFLOWS / "tests-reusable.yaml", job)
    opted = _ctx(BUMP, action="labeled", labels=OPTED, added="gate-check", needs=needs)
    plain = _ctx(
        "feat/x",
        action="labeled",
        labels=("gate-check",),
        added="gate-check",
        needs=needs,
    )
    assert evaluate(expr.replace("always() &&", "", 1), opted) is True
    assert evaluate(expr.replace("always() &&", "", 1), plain) is False


# ── SDK repo ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("job", "head_ref", "runs"),
    [
        ("sdr-k8s-e2e", BUMP, True),
        ("sdr-k8s-e2e", "bump-version-conformance", False),
        ("sdr-k8s-e2e", "bump-version-contract-toolkit", False),
        ("sdr-k8s-e2e", "feat/x", False),
        ("storage-integration", BUMP, True),
        ("storage-integration", "bump-version-conformance", False),
    ],
)
def test_sdk_tenant_e2e_follows_gate_check_on_the_sdk_lane_only(
    job: str, head_ref: str, runs: bool
) -> None:
    """Conformance / toolkit releases never pay for tenant e2e (FND-3748)."""
    ctx = _ctx(
        head_ref,
        action="labeled",
        labels=OPTED,
        added="gate-check",
        needs={
            "changes": {
                "outputs": {"sdk": "false", "container": "false", "ci": "false"}
            }
        },
    )
    assert evaluate(_job_if(WORKFLOWS / "pull_request.yaml", job), ctx) is runs


@pytest.mark.parametrize(
    "job", ["sdk-tests", "conformance", "toolkit", "conformance-suite-tests"]
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
        "container": "false",
        "force_sdk": "true",
        "force_conformance": "true",
        "force_toolkit": "true",
    }
    expr = _job_if(WORKFLOWS / "sdk-gate.yaml", job)
    assert evaluate(expr, {"needs": {"changes": {"outputs": outputs}}}) is runs


@pytest.mark.parametrize(
    ("path", "job", "step"),
    [
        (WORKFLOWS / "sdk-gate.yaml", "gate", "Require the bump-PR CI opt-in"),
        (
            WORKFLOWS / "pull_request.yaml",
            "connector-gate",
            "Require the bump-PR CI opt-in",
        ),
    ],
)
@pytest.mark.parametrize(
    ("opted_in", "fails"), [("false", True), ("true", False), ("", True)]
)
def test_sdk_inline_gates_fail_closed_until_opted_in(
    path: Path, job: str, step: str, opted_in: str, fails: bool
) -> None:
    """Fails rather than skips: a skipped required check reads as a pass."""
    found = _step(path, job, step)
    ctx = {
        "needs": {"changes": {"result": "success", "outputs": {"opted_in": opted_in}}}
    }
    assert evaluate(_strip(found["if"]), ctx) is fails
    assert "exit 1" in found["run"]
    steps = _load(path)["jobs"][job]["steps"]
    assert steps.index(found) == 0, "must fail before anything reads skips as passes"


@pytest.mark.parametrize("workflow", ["sdk-gate.yaml", "pull_request.yaml"])
def test_sdk_opt_in_reads_live_labels(workflow: str) -> None:
    step = _step(WORKFLOWS / workflow, "changes", "Check the bump-PR CI opt-in")
    assert "bump_pr_opt_in.py check" in step["run"]


def test_sdk_gate_does_not_subscribe_to_labeled() -> None:
    """Its required check is inline: a label-event skip would read as a pass."""
    on = _load(WORKFLOWS / "sdk-gate.yaml")[True]
    assert "labeled" not in (on["pull_request"] or {}).get("types", [])


@pytest.mark.parametrize(
    ("head_ref", "added", "runs"),
    [
        (BUMP, "gate-check", True),
        ("bump-version-conformance", "gate-check", True),
        (BUMP, "size/S", False),
        ("feat/x", "gate-check", False),
    ],
)
def test_gate_check_reruns_sdk_gate(head_ref: str, added: str, runs: bool) -> None:
    path = WORKFLOWS / "bump-pr-gate-check.yaml"
    ctx = _ctx(head_ref, action="labeled", labels=(added,), added=added)
    assert evaluate(_job_if(path, "rerun-sdk-gate"), ctx) is runs
    run = _load(path)["jobs"]["rerun-sdk-gate"]["steps"][-1]["run"]
    assert "--workflow sdk-gate.yaml" in run


@pytest.mark.parametrize("job", ["analyze", "lint"])
@pytest.mark.parametrize(
    ("ctx", "runs"),
    [
        (_ctx("feat/x"), True),
        (_ctx(BUMP, labels=("release",)), False),
        (_ctx(BUMP, action="labeled", labels=OPTED, added="gate-check"), True),
    ],
)
def test_codeql_waits_for_the_opt_in_on_bump_prs(
    job: str, ctx: dict[str, Any], runs: bool
) -> None:
    assert evaluate(_job_if(WORKFLOWS / "codeql.yaml", job), ctx) is runs


@pytest.mark.parametrize(
    ("head_ref", "runs"), [("feat/x", True), (BUMP, False), ("", True)]
)
def test_script_tests_never_run_on_bump_prs(head_ref: str, runs: bool) -> None:
    assert (
        evaluate(_job_if(WORKFLOWS / "scripts-tests.yaml", "pytest"), _ctx(head_ref))
        is runs
    )


# ── The opt-in is cleared before every bump push ──────────────────────────────


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("release.yaml", "bump-version"), ("release-version-bump.yaml", "bump-version")],
)
def test_release_bump_clears_the_opt_in_before_pushing(workflow: str, job: str) -> None:
    steps = _load(WORKFLOWS / workflow)["jobs"][job]["steps"]
    names = [s.get("name") for s in steps]
    reset = names.index("Reset the bump PR's CI opt-in")
    push = names.index("Commit and push bump branch")
    assert reset < push
    assert "bump_pr_opt_in.py clear" in steps[reset]["run"]
    # Runs exactly when the push does.
    assert steps[push]["if"] in steps[reset]["if"]


@pytest.mark.parametrize(
    "workflow", ["conformance-release.yml", "contract-toolkit-release.yml"]
)
def test_subpackage_release_clears_the_opt_in_before_pushing(workflow: str) -> None:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    clear = text.index("bump_pr_opt_in.py clear")
    assert clear < text.index('git push origin "${BUMP_BRANCH}"')

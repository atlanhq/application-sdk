"""Workflow gates for scanning the bump PR and promoting its image (FND-3328).

Evaluates the real `if:` / `with:` expressions out of the workflow YAML and the
bootstrap template, in both directions:

* build-and-scan: `scope` runs only on a PR / queue entry building the caller's
  own tree, and `build` / `security-gate` skip ONLY on its literal 'false' —
  skipped, so the required contexts still report;
* the template: the candidate builds only on the `/gate-check` dispatch of a
  bump-version branch (FND-3748, FND-3839), and the scan job runs on every PR
  and on that dispatch whatever the candidate did;
* build-and-publish-app: candidate mode stops after merge with no Docker Hub,
  scan, deploy or publish; a release with a scanned candidate skips the build
  and still reaches merge.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bump_skip import without_bump_skip  # noqa: E402
from _gha_expr import evaluate, evaluate_operand  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TEMPLATE = (
    REPO_ROOT
    / "packages/conformance/conformance/bootstrap/templates/vulnerability-scan.yml"
)
FAILURE_PREFIX = "!failure() && "
SOURCE = "ghcr.io/atlanhq/x@sha256:ab"


def _jobs(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


def _template_jobs() -> dict[str, Any]:
    # The default render has no `<% %>` slot lines left in it once stripped.
    lines = [
        line
        for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("<%")
    ]
    return yaml.safe_load("\n".join(lines))["jobs"]


def _strip(expression: str) -> str:
    source = expression.strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2].strip()
    return source


def _after_failure(expression: str) -> str:
    source = _strip(expression)
    assert source.startswith(FAILURE_PREFIX), source
    return source[len(FAILURE_PREFIX) :]


# ── build-and-scan.yaml ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("event", "image", "ref", "runs"),
    [
        ("pull_request", "", "", True),
        ("merge_group", "", "", True),
        ("pull_request", "ghcr.io/atlanhq/x:candidate-a@sha256:b", "", False),
        ("pull_request", "", "abc", False),
        ("push", "", "", False),
        ("workflow_dispatch", "", "", False),
    ],
)
def test_scope_runs_only_on_a_pr_or_queue_entry_building_its_own_tree(
    event: str, image: str, ref: str, runs: bool
) -> None:
    contexts = {"github": {"event_name": event}, "inputs": {"image": image, "ref": ref}}
    assert evaluate(_jobs("build-and-scan.yaml")["scope"]["if"], contexts) is runs


@pytest.mark.parametrize("job_id", ["build", "security-gate"])
@pytest.mark.parametrize("answer", ["", "true", "false"])
def test_scan_skips_only_on_scope_false(job_id: str, answer: str) -> None:
    job = _jobs("build-and-scan.yaml")[job_id]
    assert "scope" in job["needs"]
    contexts = {
        "needs": {
            "scope": {"outputs": {"scan": answer}},
            "queue-diff": {"outputs": {"image_changed": ""}},
        }
    }
    assert evaluate(without_bump_skip(job["if"]), contexts) is (answer != "false")


def test_queue_diff_waits_on_scope_and_skips_with_it() -> None:
    job = _jobs("build-and-scan.yaml")["queue-diff"]
    base = {
        "github": {"event_name": "merge_group"},
        "inputs": {"image": "", "ref": ""},
    }
    for answer, runs in (("", True), ("true", True), ("false", False)):
        contexts = {**base, "needs": {"scope": {"outputs": {"scan": answer}}}}
        assert evaluate(job["if"], contexts) is runs


@pytest.mark.parametrize(
    ("mark", "blocking", "runs"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_candidate_is_marked_only_behind_a_blocking_gate(
    mark: bool, blocking: bool, runs: bool
) -> None:
    (step,) = [
        s
        for s in _jobs("build-and-scan.yaml")["security-gate"]["steps"]
        if s.get("name") == "Mark the scanned release candidate"
    ]
    source = _strip(step["if"])
    prefix = "success() && "
    assert source.startswith(prefix)
    contexts = {"inputs": {"mark_scanned": mark, "fail_on_findings": blocking}}
    assert evaluate(source[len(prefix) :], contexts) is runs
    assert step["run"].strip().endswith("release_candidate.py mark")


# ── vulnerability-scan.yml template ───────────────────────────────────────────


def _gh(
    event: str,
    head_ref: str,
    *,
    action: str = "synchronize",
    labels: tuple[str, ...] = (),
    ref_name: str = "",
    actor: str = "someone",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {"labels": [{"name": n} for n in labels]},
    }
    return {
        "github": {
            "event_name": event,
            "head_ref": head_ref,
            "ref_name": ref_name,
            "actor": actor,
            "event": payload,
        }
    }


def _gate_check(branch: str, actor: str = "someone") -> dict[str, Any]:
    """The `/gate-check` dispatch: on the branch itself, so no `head_ref`."""
    return {
        "github": {
            "event_name": "workflow_dispatch",
            "head_ref": "",
            "ref_name": branch,
            "actor": actor,
            "event": {},
        }
    }


@pytest.mark.parametrize(
    ("ctx", "runs"),
    [
        # Bump PR: only on the `/gate-check` dispatch (FND-3748, FND-3839)…
        (_gate_check("bump-version-main"), True),
        (_gate_check("bump-version-conformance"), True),
        # …never on the bump PR's own run, whatever labels it carries: a
        # label no longer opts in.
        (_gh("pull_request", "bump-version-main"), False),
        (_gh("pull_request", "bump-version-main", labels=("gate-check",)), False),
        (_gh("pull_request", "bump-version-main", labels=("e2e",)), False),
        # A dispatch of any other branch is a manual run, not a release gate.
        (_gate_check("main"), False),
        (_gate_check("renovate/foo"), False),
        (_gh("pull_request", "renovate/foo", labels=("gate-check",)), False),
        (_gh("merge_group", ""), False),
        (_gate_check("bump-version-main", actor="dependabot[bot]"), False),
    ],
)
def test_template_builds_a_candidate_only_on_a_gate_check_dispatch(
    ctx: dict[str, Any], runs: bool
) -> None:
    job = _template_jobs()["candidate"]
    assert evaluate(job["if"], ctx) is runs
    assert job["with"]["candidate"] is True
    # Every job of the candidate build must build the same commit.
    assert job["with"]["ref"] == "${{ github.sha }}"


@pytest.mark.parametrize(
    ("ctx", "runs"),
    [
        # Ordinary PRs and queue entries: always, so the required checks report.
        (_gh("pull_request", "feat/x", action="opened"), True),
        (_gh("pull_request", "feat/x"), True),
        (_gh("merge_group", ""), True),
        # Bump PR: the caller runs on its own run too, so every required
        # `scan / …` context reports; build-and-scan skips its jobs there
        # (`skip-on-bump-pr`, FND-3947)…
        (_gh("pull_request", "bump-version-main"), True),
        (_gh("pull_request", "bump-version-main", labels=("gate-check",)), True),
        # …and the `/gate-check` dispatch scans the candidate.
        (_gate_check("bump-version-main"), True),
        (_gh("pull_request", "feat/x", actor="dependabot[bot]"), False),
    ],
)
def test_template_scan_caller_runs_except_for_dependabot(
    ctx: dict[str, Any], runs: bool
) -> None:
    job = _template_jobs()["scan"]
    assert evaluate(_strip(job["if"]), ctx) is runs


def test_template_scan_scans_the_pinned_candidate() -> None:
    job = _template_jobs()["scan"]
    assert job["needs"] == "candidate"
    assert job["with"]["image"] == "${{ needs.candidate.outputs.candidate_image }}"
    for image, mark in (("", False), ("ghcr.io/atlanhq/x:candidate-a@sha256:b", True)):
        contexts = {"needs": {"candidate": {"outputs": {"candidate_image": image}}}}
        assert evaluate_operand(job["with"]["mark_scanned"], contexts) is mark


def test_template_candidate_grants_what_build_and_publish_app_declares() -> None:
    # A callee job asking for more than its caller grants is a startup_failure.
    granted = _template_jobs()["candidate"]["permissions"]
    workflow = yaml.safe_load(
        (WORKFLOWS / "build-and-publish-app.yaml").read_text(encoding="utf-8")
    )
    needed: dict[str, str] = dict(workflow["permissions"])
    for job in workflow["jobs"].values():
        for scope, level in (job.get("permissions") or {}).items():
            if level == "write" or scope not in needed:
                needed[scope] = level
    for scope, level in needed.items():
        assert scope in granted, scope
        assert level == "read" or granted[scope] == "write", scope


# ── build-and-publish-app.yaml ────────────────────────────────────────────────


@pytest.mark.parametrize(("promote", "runs"), [("", True), (SOURCE, False)])
def test_release_build_skips_when_a_scanned_candidate_is_promoted(
    promote: str, runs: bool
) -> None:
    body = _after_failure(_jobs("build-and-publish-app.yaml")["build"]["if"])
    contexts = {
        "needs": {
            "prepare": {"result": "success", "outputs": {"promote_source": promote}}
        }
    }
    assert evaluate(body, contexts) is runs


@pytest.mark.parametrize(
    ("build_result", "promote", "runs"),
    [
        ("success", "", True),
        ("skipped", SOURCE, True),
        ("skipped", "", False),
        ("failure", "", False),
    ],
)
def test_merge_runs_after_a_build_or_on_promotion(
    build_result: str, promote: str, runs: bool
) -> None:
    body = _after_failure(_jobs("build-and-publish-app.yaml")["merge"]["if"])
    contexts = {
        "needs": {
            "prepare": {"result": "success", "outputs": {"promote_source": promote}},
            "build": {"result": build_result},
        }
    }
    assert evaluate(body, contexts) is runs


def _merge_step(name: str) -> dict[str, Any]:
    (step,) = [
        s
        for s in _jobs("build-and-publish-app.yaml")["merge"]["steps"]
        if s.get("name") == name
    ]
    return step


@pytest.mark.parametrize(
    ("name", "promote", "runs"),
    [
        ("Create and push multi-arch manifest", "", True),
        ("Create and push multi-arch manifest", SOURCE, False),
        ("Promote the scanned release candidate", "", False),
        ("Promote the scanned release candidate", SOURCE, True),
    ],
)
def test_merge_either_assembles_or_promotes(
    name: str, promote: str, runs: bool
) -> None:
    contexts = {"needs": {"prepare": {"outputs": {"promote_source": promote}}}}
    assert evaluate(_merge_step(name)["if"], contexts) is runs


def test_promote_runs_before_the_docker_hub_copy() -> None:
    names = [
        s.get("name") for s in _jobs("build-and-publish-app.yaml")["merge"]["steps"]
    ]
    assert names.index("Promote the scanned release candidate") < names.index(
        "Push to Docker Hub (re-tag, no rebuild)"
    )


def test_promote_targets_every_tag_the_assembled_manifest_would_carry() -> None:
    assemble = _merge_step("Create and push multi-arch manifest")["env"]
    promote = _merge_step("Promote the scanned release candidate")["env"]["TAGS"]
    for key in ("GHCR_IMAGE", "GHCR_BRANCH_TAG", "RELEASE_TAGS"):
        assert assemble[key] in promote


@pytest.mark.parametrize(("candidate", "runs"), [(False, True), (True, False)])
def test_candidate_mode_does_not_run_the_nested_scan(
    candidate: bool, runs: bool
) -> None:
    body = _after_failure(_jobs("build-and-publish-app.yaml")["security-scan"]["if"])
    contexts = {
        "needs": {"merge": {"result": "success"}},
        "inputs": {"candidate": candidate},
    }
    assert evaluate(body, contexts) is runs


@pytest.mark.parametrize(
    ("candidate", "manifest", "expected"),
    [
        (False, "true", "true"),
        (False, "false", "false"),
        (True, "true", "false"),
        (True, "false", "false"),
    ],
)
def test_candidate_is_never_copied_to_docker_hub_or_deployed(
    candidate: bool, manifest: str, expected: str
) -> None:
    expression = _jobs("build-and-publish-app.yaml")["prepare"]["outputs"]["enable_sdr"]
    contexts = {
        "inputs": {"candidate": candidate},
        "steps": {"manifest": {"outputs": {"enable_sdr": manifest}}},
    }
    assert evaluate_operand(expression, contexts) == expected


def test_candidate_cannot_publish_or_dispatch() -> None:
    # Both terminal jobs need inputs.publish / a main ref, which a bump PR's
    # candidate run (a dispatch on the bump branch, publish false) never has.
    jobs = _jobs("build-and-publish-app.yaml")
    assert "inputs.publish" in jobs["publish"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["app-deployment-dispatcher"]["if"]


@pytest.mark.parametrize(
    ("release_tag", "candidate", "runs"),
    [("v1.2.3", False, True), ("", False, False), ("", True, False)],
)
def test_lookup_runs_only_on_a_release(
    release_tag: str, candidate: bool, runs: bool
) -> None:
    (step,) = [
        s
        for s in _jobs("build-and-publish-app.yaml")["prepare"]["steps"]
        if s.get("id") == "candidate_lookup"
    ]
    contexts = {"inputs": {"release_tag": release_tag, "candidate": candidate}}
    assert evaluate(step["if"], contexts) is runs

"""Guards for the real-cloud storage suite's route into a required check.

The S3 / Azure / GCS integration tests spent their life as a standalone job in
`pull_request.yaml` behind a `storage-integration` label. Nothing required that
context and nothing added the label, so the suite ran when someone remembered to
ask — which meant a change that rewrote both download paths could reach main
without a single real-cloud request.

They now live in `sdk-tests-reusable.yaml`, so their result rolls up through the
`SDK Tests` area into the `SDK Gate` required check exactly like every other SDK
test. Three things have to hold for that to be true rather than merely look it,
and each fails silently if it drifts:

* The job is in the reusable, not back in `pull_request.yaml` as its own
  unrequired context.
* It is `merge_group`-only. On `pull_request` a fork's token cannot obtain OIDC
  federation into any of the three clouds, so a PR-side arm would red the gate
  for every external contributor.
* Both the job AND every caller of the reusable grant `id-token: write`. A
  caller's `permissions:` block replaces the workflow-level grant rather than
  extending it, and a called workflow can never exceed it — so an omission here
  surfaces as three opaque cloud-login failures, not as a permissions error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github/workflows"
_REUSABLE = "sdk-tests-reusable.yaml"
_JOB = "storage-integration"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8")) or {}


def _jobs(name: str) -> dict[str, Any]:
    return _load(name).get("jobs") or {}


def test_the_suite_rolls_up_through_the_sdk_tests_area() -> None:
    """In the reusable that `SDK Gate` composes — not a context of its own."""
    assert _JOB in _jobs(_REUSABLE), (
        f"{_JOB} is gone from {_REUSABLE}. If it moved back to its own workflow "
        "it no longer reaches the SDK Gate, and no ruleset requires its context."
    )
    assert _JOB not in _jobs("pull_request.yaml"), (
        f"{_JOB} is back in pull_request.yaml as a standalone job. That context "
        "is not in the main ruleset's required checks, so it can go red without "
        "stopping a merge."
    )


def test_the_suite_is_merge_queue_only() -> None:
    """A `pull_request` arm would red the gate for fork PRs (no OIDC)."""
    condition = str(_jobs(_REUSABLE)[_JOB].get("if", ""))
    assert condition.strip() == "github.event_name == 'merge_group'", (
        "the storage-integration job must stay merge_group-only: a fork PR's "
        "token cannot federate into S3 / Azure / GCS, so a pull_request arm "
        f"fails the SDK Gate for external contributors. Found: {condition!r}"
    )


def test_the_job_declares_the_oidc_permission() -> None:
    permissions = _jobs(_REUSABLE)[_JOB].get("permissions") or {}
    assert permissions.get("id-token") == "write", (
        "the three cloud logins are keyless and mint a GitHub OIDC token; "
        f"without id-token:write they fail with an opaque error. Found: {permissions}"
    )
    assert permissions.get("contents") == "read", (
        "a job-level permissions block REPLACES the workflow-level one, so "
        "contents:read must be restated here or the checkout 403s"
    )


def test_every_caller_of_the_reusable_grants_id_token() -> None:
    """`permissions:` on a caller is a ceiling, and id-token is never default.

    Swept across all workflows rather than asserted on `sdk-gate.yaml` alone: a
    second caller added later would inherit the repo default, which grants no
    id-token at any setting, and the failure would land inside the reusable.
    """
    callers: list[str] = []
    for path in sorted(_WORKFLOW_DIR.glob("*.y*ml")):
        for job_name, job in (_load(path.name).get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            if str(job.get("uses", "")).endswith(_REUSABLE):
                callers.append(f"{path.name}:{job_name}")
                permissions = job.get("permissions") or {}
                assert permissions.get("id-token") == "write", (
                    f"{path.name}:{job_name} calls {_REUSABLE} without granting "
                    "id-token:write. The called workflow cannot exceed the "
                    "caller's grant, so storage-integration's cloud logins fail "
                    f"there rather than here. Found: {permissions}"
                )
                assert permissions.get("contents") == "read", (
                    f"{path.name}:{job_name} declares permissions but omits "
                    "contents:read; the block replaces the workflow default, so "
                    "every checkout in the reusable 403s"
                )
    assert callers, (
        f"no caller of {_REUSABLE} found — this guard is passing vacuously, so "
        "the filename it looks for has drifted"
    )

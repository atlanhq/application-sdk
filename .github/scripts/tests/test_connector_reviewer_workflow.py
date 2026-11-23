"""Guards on the workflow that runs the connector reviewer in an app repo (FND-5314).

`connector-reviewer-reusable.yaml` is called from a `/review` comment in a
connector repo. A comment-triggered run holds secrets, so its safety rests on a
few structural facts about the file, not on anyone remembering them:

* it is only ever called (`workflow_call`), never triggered by a pull request
  event in this repository;
* it never checks out pull-request code: one checkout of the caller's default
  branch, one of this repository at the workflow's own commit;
* it has no `concurrency:` block, because the reviewer has its own lock and a
  block would queue or cancel reviews;
* its job timeout equals the lock's idea of how long a review can run;
* the App token is scoped to the calling repository and to named permissions,
  and cannot write contents;
* no PAT is referenced anywhere;
* the run report is uploaded even when the review fails.

lens has the same guards for `lens.yml` in `test_lens.py`. They were not copied
with the fork because the fork ships no workflow; this file brings them back
for the workflow that runs it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_WORKFLOW = _SCRIPTS.parent / "workflows" / "connector-reviewer-reusable.yaml"

#: Must equal ``JOB_TIMEOUT`` in ``connector_reviewer/lock.py`` (checked below
#: when the package is present).
JOB_TIMEOUT_MINUTES = 45

#: Exactly what the reviewer uses on GitHub. Anything more is a wider token than
#: a review needs; `contents: write` in particular would let it push.
TOKEN_PERMISSIONS = {
    "permission-contents": "read",
    # GitHub needs pull-requests write to comment on a pull request.
    "permission-pull-requests": "write",
    "permission-issues": "write",
    "permission-statuses": "write",
    "permission-actions": "read",
}


@pytest.fixture(scope="module")
def text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text: str) -> dict[Any, Any]:
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def job(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs: dict[str, Any] = workflow["jobs"]
    assert list(jobs) == ["review"]
    return jobs["review"]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _uses(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if str(s.get("uses", "")).startswith(action + "@")]


def test_it_is_only_ever_called(workflow: dict[Any, Any]) -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers: dict[str, Any] = workflow.get("on") or workflow[True]
    assert list(triggers) == ["workflow_call"]


def test_it_never_checks_out_pull_request_code(job: dict[str, Any]) -> None:
    """Two checkouts: the caller's default branch (no `ref`, which for a
    comment event is the default branch), and this repository at the workflow's
    own commit. Neither may name a pull request ref."""
    checkouts = _uses(job, "actions/checkout")
    assert len(checkouts) == 2
    app, sdk = (c["with"] for c in checkouts)
    assert "ref" not in app and "repository" not in app
    assert sdk["repository"] == "atlanhq/application-sdk"
    assert sdk["ref"] == "${{ job.workflow_sha }}"
    for with_ in (app, sdk):
        assert with_["persist-credentials"] is False
    assert app["path"] != sdk["path"], "the app and the reviewer must not share a dir"


def test_no_step_mentions_a_pull_request_head(text: str) -> None:
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "pull_request.head" not in code
    assert "refs/pull/" not in code
    assert "head_ref" not in code


def test_there_is_no_concurrency_block(
    workflow: dict[Any, Any], job: dict[str, Any]
) -> None:
    assert "concurrency" not in workflow
    assert "concurrency" not in job


def test_the_job_timeout_is_the_locks(job: dict[str, Any]) -> None:
    assert job["timeout-minutes"] == JOB_TIMEOUT_MINUTES


def test_the_timeout_matches_the_package_when_it_is_present() -> None:
    """The workflow and the engine land in separate changes. Once both are in
    the tree this pins them together."""
    lock = _SCRIPTS / "connector_reviewer" / "lock.py"
    if not lock.is_file():
        pytest.skip("the connector_reviewer package is not in this tree yet")
    # Read as text: the two constants are plain assignments, and this test must
    # not depend on how the package is importable from here.
    source = lock.read_text(encoding="utf-8")
    minutes = re.search(r"^JOB_TIMEOUT = timedelta\(minutes=(\d+)\)$", source, re.M)
    assert minutes and int(minutes.group(1)) == JOB_TIMEOUT_MINUTES
    assert re.search(r'^WORKFLOW_FILE = "connector-reviewer\.yaml"$', source, re.M)


def test_the_app_token_is_scoped_to_the_caller_and_cannot_push(
    job: dict[str, Any],
) -> None:
    [mint] = _uses(job, "actions/create-github-app-token")
    with_: dict[str, Any] = mint["with"]
    assert with_["repositories"] == "${{ github.event.repository.name }}"
    granted = {k: v for k, v in with_.items() if k.startswith("permission-")}
    assert granted == TOKEN_PERMISSIONS


def test_the_jobs_own_token_only_reads(workflow: dict[Any, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_no_pat_is_referenced(text: str) -> None:
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    assert secrets == {
        "FLEET_APP_ID",
        "FLEET_APP_PRIVATE_KEY",
        "CONNECTOR_REVIEWER_GATEWAY_KEY",
    }


def test_the_review_reads_the_fleet_principles_from_this_repository(
    job: dict[str, Any],
) -> None:
    [review] = [s for s in _steps(job) if s.get("name") == "Review"]
    env: dict[str, str] = review["env"]
    assert env["ROOT"].endswith("/repo")
    assert env["CONNECTOR_REVIEWER_PRINCIPLES_DIR"].endswith(
        "/sdk/.github/connector-reviewer/principles"
    )
    assert "python -m connector_reviewer review" in review["run"]


def test_a_review_that_dies_is_finalized_with_the_same_token(
    job: dict[str, Any],
) -> None:
    """A crashed, timed-out or cancelled review must not leave its status
    pending: finalize runs after it whatever happened, as the same App, and
    needs no model key."""
    steps = _steps(job)
    names = [s.get("name") for s in steps]
    review = steps[names.index("Review")]
    final = steps[names.index("Finalize the status")]
    assert names.index("Finalize the status") == names.index("Review") + 1
    assert final["if"] == "always() && steps.app.outcome == 'success'"
    assert "python -m connector_reviewer finalize" in final["run"]
    assert final["working-directory"] == review["working-directory"]
    assert final["env"]["GITHUB_TOKEN"] == review["env"]["GITHUB_TOKEN"]
    assert (
        final["env"]["CONNECTOR_REVIEWER_BOT_LOGIN"]
        == review["env"]["CONNECTOR_REVIEWER_BOT_LOGIN"]
    )
    assert "CONNECTOR_REVIEWER_GATEWAY_KEY" not in final["env"]


def test_the_review_step_times_out_before_the_job(job: dict[str, Any]) -> None:
    """Otherwise the job's own timeout kills the finalize step with it."""
    [review] = [s for s in _steps(job) if s.get("name") == "Review"]
    assert 0 < review["timeout-minutes"] < JOB_TIMEOUT_MINUTES


def test_the_run_report_is_uploaded_even_when_the_review_fails(
    job: dict[str, Any],
) -> None:
    uploads = _uses(job, "actions/upload-artifact")
    assert len(uploads) == 2
    assert all(str(u["if"]).startswith("always()") for u in uploads)
    assert all(u.get("continue-on-error") is True for u in uploads)
    names = [u["with"]["name"] for u in uploads]
    assert names[0] != names[1], "a retry must use a different artifact name"

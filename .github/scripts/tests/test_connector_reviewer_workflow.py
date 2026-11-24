"""Guards on the workflow that runs the connector reviewer in an app repo (FND-5314).

`connector-reviewer-reusable.yaml` is called from a `/review` comment in a
connector repo. A comment-triggered run holds secrets, so its safety rests on a
few structural facts about the file, not on anyone remembering them:

* it is only ever called (`workflow_call`), never triggered by a pull request
  event in this repository;
* the job that holds the secrets (`review`) never checks out pull-request
  code and installs nothing: one checkout of the caller's principles at its
  default branch, one of the caller at the pull request's base commit
  (FND-5316), one of this repository at the workflow's own commit, and one of
  this repository's `application_sdk/` at the ref the pull request head's
  uv.lock pins (FND-5331, FND-5316: read through the API as data, never
  checked out), skipped when none resolves and never fatal;
* the gate-file check (FND-5325) is a job of its own (`gate`) that holds no
  secret and no App token: it alone checks out the pull request head (sparse,
  read as data) and installs the pinned conformance package, and hands the
  review a fact file through an artifact;
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
    assert list(jobs) == ["gate", "review"]
    return jobs["review"]


@pytest.fixture(scope="module")
def gate_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    return workflow["jobs"]["gate"]


@pytest.fixture(scope="module")
def gate_text(text: str) -> str:
    """The `gate` job's lines of the workflow, comments dropped."""
    start = text.index("\n  gate:\n")
    end = text.index("\n  review:\n")
    return "\n".join(
        ln for ln in text[start:end].splitlines() if not ln.lstrip().startswith("#")
    )


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _uses(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if str(s.get("uses", "")).startswith(action + "@")]


def test_it_is_only_ever_called(workflow: dict[Any, Any]) -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers: dict[str, Any] = workflow.get("on") or workflow[True]
    assert list(triggers) == ["workflow_call"]


def _checkout(job: dict[str, Any], path: str) -> dict[str, Any]:
    [step] = [
        c for c in _uses(job, "actions/checkout") if c["with"].get("path") == path
    ]
    return step


def test_it_never_checks_out_pull_request_code(job: dict[str, Any]) -> None:
    """Four checkouts: the caller's principles from its default branch, the
    caller at the pull request's base commit, this repository at the
    workflow's own commit, and this repository's SDK source at the version the
    app pins. None may name a pull request ref, and each has a directory of
    its own."""
    checkouts = _uses(job, "actions/checkout")
    assert len(checkouts) == 4
    principles = _checkout(job, "app-principles")["with"]
    app = _checkout(job, "repo")["with"]
    sdk = _checkout(job, "sdk")["with"]
    src = _checkout(job, "sdk-src")["with"]
    # Exactly two checkouts of the app, each at a ref it names.
    assert [c for c in checkouts if "repository" not in c["with"]] == [
        _checkout(job, "app-principles"),
        _checkout(job, "repo"),
    ]
    assert principles["ref"] == "${{ github.event.repository.default_branch }}"
    assert app["ref"] == "${{ steps.base-ref.outputs.base }}"
    assert sdk["repository"] == "atlanhq/application-sdk"
    assert sdk["ref"] == "${{ job.workflow_sha }}"
    assert src["repository"] == "atlanhq/application-sdk"
    for with_ in (principles, app, sdk, src):
        assert with_["persist-credentials"] is False
        assert "token" not in with_
    assert len({w["path"] for w in (principles, app, sdk, src)}) == 4


def test_the_code_context_is_the_pull_requests_base_commit(
    job: dict[str, Any],
) -> None:
    """FND-5316: the app was checked out at its default branch, so a pull
    request against another branch, or a replay of an old one, was reviewed
    against today's default branch: unchanged files, callers and facts came
    from code the pull request never saw. The checkout now takes the base
    commit the resolve step reads through the API, in tested Python, after the
    App token is minted (the event of a comment does not carry the base)."""
    steps = _steps(job)
    [resolve] = [s for s in steps if s.get("id") == "base-ref"]
    [mint] = _uses(job, "actions/create-github-app-token")
    app = _checkout(job, "repo")
    assert steps.index(mint) < steps.index(resolve) < steps.index(app)
    assert "python -m connector_reviewer base-ref" in resolve["run"]
    assert resolve["working-directory"] == "sdk/.github/scripts"
    assert resolve.get("continue-on-error") is True
    assert not re.search(r"\b(if|case|for|while)\b", resolve["run"])
    for arg in (
        '--repo "$REPO"',
        '--event-name "$EVENT_NAME"',
        '--event-path "$EVENT_PATH"',
    ):
        assert arg in resolve["run"]
    assert resolve["env"] == {
        "GITHUB_TOKEN": "${{ steps.app.outputs.token }}",
        "REPO": "${{ github.repository }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "EVENT_PATH": "${{ github.event_path }}",
    }
    # The setup-python step and the reviewer code come first: it runs Python.
    setup = _uses(job, "actions/setup-python")[0]
    assert steps.index(setup) < steps.index(resolve)
    assert steps.index(_checkout(job, "sdk")) < steps.index(resolve)
    [review] = [s for s in steps if s.get("name") == "Review"]
    assert steps.index(app) < steps.index(review)


def test_the_apps_principles_come_from_its_default_branch_alone(
    job: dict[str, Any],
) -> None:
    """The principles a pull request is judged by are read from the default
    branch, as documented, never from the base checkout: neither the pull
    request nor the branch it targets can rewrite them. The checkout holds
    nothing else."""
    principles = _checkout(job, "app-principles")["with"]
    assert principles["sparse-checkout"].split() == [
        ".github/connector-reviewer/principles"
    ]
    assert principles["sparse-checkout-cone-mode"] is False
    [review] = [s for s in _steps(job) if s.get("name") == "Review"]
    assert review["env"]["CONNECTOR_REVIEWER_PRINCIPLES_ROOT"] == (
        "${{ github.workspace }}/app-principles"
    )


def test_the_sdk_source_is_checked_out_at_the_resolved_pin(
    job: dict[str, Any],
) -> None:
    """The SDK source checkout takes its ref from the resolve step, written by
    tested Python (`connector_reviewer sdk-ref`), not from workflow shell. It
    runs only when a ref resolved, reads `application_sdk/` alone, and a ref
    that does not exist cannot fail the job: the review runs without it."""
    steps = _steps(job)
    [resolve] = [s for s in steps if s.get("id") == "sdk-ref"]
    assert "python -m connector_reviewer sdk-ref" in resolve["run"]
    assert resolve["working-directory"] == "sdk/.github/scripts"
    assert resolve.get("continue-on-error") is True
    # Straight-line: no shell branching in the step (docs/standards/ci.md).
    assert not re.search(r"\b(if|case|for|while)\b", resolve["run"])
    src = _checkout(job, "sdk-src")
    assert src["if"] == "steps.sdk-ref.outputs.ref != ''"
    assert src["with"]["ref"] == "${{ steps.sdk-ref.outputs.ref }}"
    assert src["with"]["sparse-checkout"].split() == ["/application_sdk/"]
    assert src["with"]["sparse-checkout-cone-mode"] is False
    assert src.get("continue-on-error") is True
    assert steps.index(resolve) < steps.index(src)
    # The setup-python step comes first: the resolve step runs Python.
    setup = _uses(job, "actions/setup-python")[0]
    assert steps.index(setup) < steps.index(resolve)
    [review] = [s for s in steps if s.get("name") == "Review"]
    assert steps.index(src) < steps.index(review)
    assert review["env"]["CONNECTOR_REVIEWER_SDK_SOURCE"] == (
        "${{ github.workspace }}/sdk-src"
    )


def test_the_sdk_pin_is_read_from_the_pull_request_head_not_the_default_branch(
    job: dict[str, Any],
) -> None:
    """FND-5316: the resolve step reads the pull request head's uv.lock through
    the API (the pull request from the event), not the default-branch checkout,
    whose lock named the wrong SDK for a pull request that pins another one.
    Reading the pull request needs the App token, so the step comes after the
    mint; the SDK checkout itself keeps the job's own read-only token."""
    steps = _steps(job)
    [resolve] = [s for s in steps if s.get("id") == "sdk-ref"]
    [mint] = _uses(job, "actions/create-github-app-token")
    assert steps.index(mint) < steps.index(resolve)
    assert "--root" not in resolve["run"]
    for arg in (
        '--repo "$REPO"',
        '--event-name "$EVENT_NAME"',
        '--event-path "$EVENT_PATH"',
    ):
        assert arg in resolve["run"]
    assert resolve["env"] == {
        "GITHUB_TOKEN": "${{ steps.app.outputs.token }}",
        "REPO": "${{ github.repository }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "EVENT_PATH": "${{ github.event_path }}",
    }
    assert "token" not in _checkout(job, "sdk-src")["with"]
    # The review is told which head the checkout's ref came from (lens
    # F-f65a20): it re-reads the lock at the head it reviews and names both
    # heads when a push in between moved the pin.
    [review] = [s for s in steps if s.get("name") == "Review"]
    assert review["env"]["CONNECTOR_REVIEWER_SDK_HEAD"] == (
        "${{ steps.sdk-ref.outputs.head }}"
    )


def test_no_step_mentions_a_pull_request_head(text: str, gate_text: str) -> None:
    """Outside the gate job (FND-5325), which reads the head as data, nothing
    names a pull request head."""
    code = "\n".join(
        line
        for line in text.replace(gate_text, "").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "pull_request.head" not in code
    assert "refs/pull/" not in code
    assert "head_ref" not in code


def test_the_secret_holding_job_never_names_a_pull_request_ref(
    job: dict[str, Any],
) -> None:
    """FND-5325 checks out the pull request head, in the `gate` job only. The
    review job holds the App key and the model key: none of its checkouts may
    take a ref from the gate job, and none may install anything."""
    for step in _uses(job, "actions/checkout"):
        assert "gate-ref" not in str(step["with"].get("ref", ""))
    runs = " ".join(str(s.get("run", "")) for s in _steps(job))
    for installer in ("pip", "uv ", "venv", "gate-fact"):
        assert installer not in runs


def test_the_gate_job_holds_no_secret_and_no_app_token(
    workflow: dict[Any, Any], gate_job: dict[str, Any], gate_text: str
) -> None:
    """The gate job is where third-party code runs (the pinned conformance
    package and its dependencies) and where the head is checked out. It must
    have nothing to steal: no secret, no App token, no write scope, and no
    credentials left in a checkout."""
    assert "secrets." not in gate_text
    assert "create-github-app-token" not in gate_text
    assert "GITHUB_TOKEN" not in gate_text
    assert "permissions" not in gate_job  # the workflow's contents: read
    assert workflow["permissions"] == {"contents": "read"}
    for step in _uses(gate_job, "actions/checkout"):
        assert step["with"]["persist-credentials"] is False
        assert "token" not in step["with"]


def test_the_gate_job_checks_out_the_head_sparse_from_a_tested_ref(
    gate_job: dict[str, Any],
) -> None:
    """The head ref comes from tested Python (`gate-ref`), never from an
    expression on the event, and the checkout holds only what the check
    reads: the CI directories, renovate.json and uv.lock."""
    steps = _steps(gate_job)
    [resolve] = [s for s in steps if s.get("id") == "gate-ref"]
    assert "python -m connector_reviewer gate-ref" in resolve["run"]
    assert resolve["working-directory"] == "sdk/.github/scripts"
    assert not re.search(r"\b(if|case|for|while)\b", resolve["run"])
    [head] = [
        c
        for c in _uses(gate_job, "actions/checkout")
        if c["with"].get("path") == "gate-head"
    ]
    assert head["if"] == "steps.gate-ref.outputs.ref != ''"
    assert head["with"]["ref"] == "${{ steps.gate-ref.outputs.ref }}"
    assert "repository" not in head["with"]
    assert head["with"]["sparse-checkout"].split() == [
        "/.github/workflows/",
        "/.github/actions/",
        "/.github/scripts/",
        "/renovate.json",
    ]
    assert head["with"]["sparse-checkout-cone-mode"] is False
    assert head.get("continue-on-error") is True
    assert steps.index(resolve) < steps.index(head)
    [sdk] = [
        c for c in _uses(gate_job, "actions/checkout") if c["with"].get("path") == "sdk"
    ]
    assert sdk["with"]["repository"] == "atlanhq/application-sdk"
    assert sdk["with"]["ref"] == "${{ job.workflow_sha }}"
    # The base branch's uv.lock alone: its pin is the version rendered.
    [base] = [
        c
        for c in _uses(gate_job, "actions/checkout")
        if c["with"].get("path") == "gate-base"
    ]
    assert base["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert base["with"]["sparse-checkout"].split() == ["/uv.lock"]
    assert "repository" not in base["with"]
    assert base.get("continue-on-error") is True
    assert len(_uses(gate_job, "actions/checkout")) == 3


def test_the_gate_fact_reaches_the_review_and_never_blocks_it(
    gate_job: dict[str, Any], job: dict[str, Any]
) -> None:
    """The fact step always exits 0; its file is uploaded (with the repo's
    retry) and downloaded by the review, which runs after the gate job
    whatever its outcome, short of a cancel."""
    steps = _steps(gate_job)
    [fact] = [s for s in steps if "gate-fact" in str(s.get("run", ""))]
    assert fact["working-directory"] == "sdk/.github/scripts"
    assert not re.search(r"\b(if|case|for|while)\b", fact["run"])
    assert "env" not in fact  # nothing handed to it, no token among it
    out = "$RUNNER_TEMP/gate-fact/connector-reviewer-gate.json"
    assert f'--out "{out}"' in fact["run"]
    assert '--base-checkout "$GITHUB_WORKSPACE/gate-base"' in fact["run"]
    uploads = _uses(gate_job, "actions/upload-artifact")
    assert len(uploads) == 2
    assert all(u.get("continue-on-error") is True for u in uploads)
    assert [u["with"]["name"] for u in uploads] == [
        "connector-reviewer-gate",
        "connector-reviewer-gate-retry",
    ]
    assert job["needs"] == "gate"
    assert job["if"] == "${{ !cancelled() }}"
    download, retry = _uses(job, "actions/download-artifact")
    for step in (download, retry):
        assert step.get("continue-on-error") is True
        assert step["with"] == download["with"]
    assert download["with"]["pattern"] == "connector-reviewer-gate*"
    assert download["with"]["merge-multiple"] is True
    assert retry["if"] == "steps.download-gate.outcome == 'failure'"
    review_steps = _steps(job)
    [review] = [s for s in review_steps if s.get("name") == "Review"]
    assert review_steps.index(download) < review_steps.index(review)
    assert review["env"]["CONNECTOR_REVIEWER_GATE_FACT"] == (
        "${{ runner.temp }}/gate-fact/connector-reviewer-gate.json"
    )
    # The setup-python step comes before the Python steps.
    [setup] = _uses(gate_job, "actions/setup-python")
    assert steps.index(setup) < steps.index(fact)


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

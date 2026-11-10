#!/usr/bin/env python3
"""The unit-tests gate of build-and-publish-app.yaml (job ``unit-tests-gate``).

The job is permanently ``if: false`` (#1943: certify runs the tests itself) but
is kept for backward compatibility, so its logic still lives here rather than
inline. Subcommands:

``prepare``
    Resolve the publish SHA, reuse an existing run of ``WORKFLOW_FILE`` on it or
    dispatch one, and write its ``run_id``. Reads ``REF``, ``DISPATCH_REF``,
    ``WORKFLOW_FILE``, ``REPO``.

``wait``
    Poll ``RUN_ID`` until it completes and write its ``conclusion`` and ``url``.

Contract for the app's workflow: docs/standards/unit-tests-gate.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time

import _bap

DISPATCH_POLL_ATTEMPTS = 12
DISPATCH_POLL_SLEEP_S = 5
WAIT_ITERATIONS = 70
WAIT_SLEEP_S = 25
CONTRACT_URL = (
    "https://github.com/atlanhq/application-sdk/blob/main/docs/standards/"
    "unit-tests-gate.md"
)


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _run_list(repo: str, workflow: str, *filters: str) -> str:
    return _bap.run_checked(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            *filters,
            "--limit",
            "1",
            "--json",
            "databaseId",
            "--jq",
            ".[0].databaseId // empty",
        ]
    )


def prepare() -> int:
    ref = _bap.env("REF")
    dispatch_ref = _bap.env("DISPATCH_REF")
    workflow = _bap.env("WORKFLOW_FILE")
    repo = _bap.env("REPO")

    sha = _bap.run_checked(
        ["gh", "api", f"/repos/{repo}/commits/{ref}", "--jq", ".sha"]
    )
    print(f"Resolved '{ref}' → {sha}")

    # Any conclusion: the next step inspects the result.
    existing = _run_list(repo, workflow, "--commit", sha)
    if existing:
        print(f"Found existing run {existing} of '{workflow}' on {sha} — reusing.")
        _bap.output(run_id=existing)
        return 0

    print(
        f"No existing run of '{workflow}' on {sha}. Dispatching on '{dispatch_ref}'..."
    )
    # `gh run list --branch` matches the short headBranch, not the full ref.
    branch = _strip_prefix(_strip_prefix(dispatch_ref, "refs/heads/"), "refs/tags/")
    before = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dispatched = _bap.run(
        ["gh", "workflow", "run", workflow, "--repo", repo, "--ref", dispatch_ref]
    )
    if dispatched.returncode != 0:
        _bap.summary(
            f"### Publish blocked: failed to dispatch '{workflow}'",
            "",
            f"The gate tried to dispatch `{workflow}` on `{dispatch_ref}` for SHA "
            f"`{sha}` but the dispatch failed.",
            "",
            "Common causes:",
            "- The workflow does not declare `workflow_dispatch:` in its `on:` block.",
            f"- The file does not exist at `.github/workflows/{workflow}`.",
            f"- The branch `{dispatch_ref}` does not exist.",
            "",
            f"Contract + template: <{CONTRACT_URL}>",
        )
        print(
            f"::error title=Publish blocked::Failed to dispatch '{workflow}'. "
            "See workflow summary."
        )
        return 1

    # `gh workflow run` doesn't return the run ID; list recent dispatches.
    print("Dispatched. Polling for the new run to appear...")
    run_id = ""
    for attempt in range(1, DISPATCH_POLL_ATTEMPTS + 1):
        time.sleep(DISPATCH_POLL_SLEEP_S)
        run_id = _run_list(
            repo,
            workflow,
            "--branch",
            branch,
            "--event",
            "workflow_dispatch",
            "--created",
            f">{before}",
        )
        if run_id:
            print(f"New run registered after {attempt} attempt(s): {run_id}")
            break
        print(
            f"Attempt {attempt}/{DISPATCH_POLL_ATTEMPTS} — run not yet visible, retrying..."
        )

    if not run_id:
        print(
            f"::error title=Publish blocked::Dispatched '{workflow}' but the new run "
            "never registered within 60s."
        )
        return 1

    _bap.output(run_id=run_id)
    return 0


def wait() -> int:
    run_id = _bap.env("RUN_ID")
    repo = _bap.env("REPO")
    print(f"Polling run {run_id} every 30s (timeout 30m)...")
    # The job timeout is the outer cap; this bounds a `gh` that returns junk.
    for iteration in range(1, WAIT_ITERATIONS + 1):
        result = json.loads(
            _bap.run_checked(
                [
                    "gh",
                    "run",
                    "view",
                    run_id,
                    "--repo",
                    repo,
                    "--json",
                    "status,conclusion,url",
                ]
            )
        )
        status = _bap.jq_str(result.get("status"))
        conclusion = result.get("conclusion") or ""
        url = _bap.jq_str(result.get("url"))
        print(f"  iteration {iteration}: status={status} conclusion={conclusion}")
        if status == "completed":
            _bap.output(conclusion=conclusion, url=url)
            return 0
        time.sleep(WAIT_SLEEP_S)
    print(
        f"::error title=Publish blocked::Tests run {run_id} did not complete within "
        "the gate timeout."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "wait"))
    args = parser.parse_args(argv)
    try:
        return prepare() if args.command == "prepare" else wait()
    except _bap.CommandFailed as exc:
        return exc.returncode


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())

"""Decide whether a release PR has passed its release gate.

A release PR (a ``bump-version*`` branch, labelled ``release`` by the SDK's
release-version-bump workflow) may not merge until its full release gate has
run AND passed on its head commit. Every other PR passes. The caller fetches
the head commit's ``gate-check`` status in a separate, ``continue-on-error``
step and passes the state in a file::

    python .github/scripts/release_gate.py \
      --head-ref "$HEAD_REF" --labels "$LABELS" \
      --gate-check-state-file gate-check-state.txt

Every argument is passed unconditionally so the caller's ``run:`` block
stays straight-line (per docs/standards/ci.md): no ``if``, no ``||``, no
parameter-expansion tricks. All the branching is here, where a test can
reach it, and none of it touches the network.

The ``gate-check`` status is the release gate's verdict (FND-3839). A
``/gate-check`` PR comment posts it as ``pending`` and dispatches the gated
workflows; once the last of them finishes it becomes ``success`` only if all
of them passed, and this gate is re-run to read it. So:

* ``success`` passes;
* ``pending`` fails - the gate is still running;
* ``failure`` / ``error`` fail - something in the gate failed;
* no status fails - nobody has asked yet.

Labels no longer pass this gate. They used to (``gate-check`` / ``e2e``), but
a label is not a verdict: it said a run was requested, and the run itself was
started by the label's ``labeled`` event, which also started duplicate runs on
the same commit. The status is bound to the commit, so a push moves the PR onto
a commit with no status and the gate re-blocks until ``/gate-check`` is asked
again on the new head.

Only a status ``github-actions[bot]`` created counts (FND-3947). That is the
identity ``test-commands.yaml`` posts the verdict with (``github.token``).
Anyone with write access can post a ``gate-check`` status through the API, and
on a bump PR this gate is the only blocker: the gated reusables skip their jobs
there, and a skipped required check reads as a pass. So a status from anyone
else is ignored, never read as a verdict.

An empty or unreadable state file means the lookup failed, and the gate
FAILS on it. That is the opposite of the connector-review gate, and
deliberate: a release that skips its gate is worse than a re-run of a
two-minute check.

``bootstrap`` vendors this file into every consumer repo and overwrites
it on every run, so a consumer cannot fix it locally - the next
bootstrap reverts the fix. It therefore has to be lint-clean under the
strictest ruff config in the fleet, not just this repo's (FND-445).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

RELEASE_LABEL = "release"
BUMP_BRANCH_PREFIX = "bump-version"
VERDICT_CREATOR = "github-actions[bot]"

# Kept at module level so each line fits inside the tightest wrap in the
# fleet and `ruff format` is a no-op at every width (see FND-445).
MISSING_HINT = "Release PR CI is opt-in. Comment /gate-check to run it."
PENDING_HINT = "The release gate is still running on this commit."
FAILED_HINT = "The release gate failed on this commit. Fix it, then /gate-check."
PASSED = "The release gate passed on this commit - gate passes."


def parse_labels(raw: str) -> list[str]:
    """Return label names from ``toJson(...labels.*.name)``, else ``[]``."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [label for label in payload if isinstance(label, str)]


def read_state(path: str) -> str:
    """Return the newest ``gate-check`` state ``VERDICT_CREATOR`` posted, else ``""``.

    ``gh api .../statuses --jq`` prints one ``<state> <creator login>`` line
    per status, newest first. The first line whose creator is
    ``VERDICT_CREATOR`` is the verdict. A line from anyone else, or in any
    other shape, is skipped.
    """
    file = pathlib.Path(path)
    if not path or not file.is_file():
        return ""
    for line in file.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == VERDICT_CREATOR:
            return fields[0]
    return ""


def is_release_pr(head_ref: str, labels: list[str]) -> bool:
    """Return whether this is a release PR: a bump branch or ``release`` label.

    The branch is the reliable signal: the label is added after the PR is
    opened, so the ``opened`` event's payload can lack it.
    """
    return head_ref.startswith(BUMP_BRANCH_PREFIX) or RELEASE_LABEL in labels


def decide(head_ref: str, labels: list[str], state: str) -> tuple[bool, str]:
    """Return (passes, message) for the head commit's ``gate-check`` state.

    ``state`` is ``""`` when there is no status or it could not be read.
    """
    if not is_release_pr(head_ref, labels):
        return True, "Not a release PR - gate passes."
    if state == "success":
        return True, PASSED
    if state == "pending":
        return False, PENDING_HINT
    if state in {"failure", "error"}:
        return False, FAILED_HINT
    return False, MISSING_HINT


def _build_parser() -> argparse.ArgumentParser:
    """Build the gate's argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-ref", default="", help="the PR's head branch")
    parser.add_argument(
        "--labels",
        default="[]",
        help="the PR's label names as a JSON array",
    )
    parser.add_argument(
        "--gate-check-state-file",
        default="",
        help="file holding the head commit's `gate-check` status state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the gate and return the process exit code."""
    args = _build_parser().parse_args(argv)
    passes, message = decide(
        args.head_ref,
        parse_labels(args.labels),
        read_state(args.gate_check_state_file),
    )
    if passes:
        print(message)  # noqa: T201
        return 0
    print(f"::error::{message}")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())

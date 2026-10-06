"""Decide whether a release PR has had its end-to-end run.

A release PR (labelled ``release`` by the SDK's release-version-bump
workflow) may not merge until the e2e suite has run against it. Every
other PR passes. The caller fetches the head commit's ``e2e`` status in a
separate, ``continue-on-error`` step and passes the state in a file::

    python .github/scripts/release_gate.py \
      --labels "$LABELS" --e2e-state-file e2e-state.txt

Every argument is passed unconditionally so the caller's ``run:`` block
stays straight-line (per docs/standards/ci.md): no ``if``, no ``||``, no
parameter-expansion tricks. All the branching is here, where a test can
reach it, and none of it touches the network.

Two signals count as "e2e has run":

* the ``e2e`` label is on the PR - a run has been requested, and the
  required ``tests / Tests Gate`` check enforces its result; or
* the head commit carries a successful ``e2e`` commit status.

The second exists because the SDK's Tests Gate consumes the label once the
run finishes (FND-3411): left in place, every later push re-ran the
live-tenant suite. The status is bound to the commit, so a push moves the
PR onto a commit with no status and the gate re-blocks until someone adds
the label again - the same outcome a missing label always had.

An empty or unreadable state file means the lookup failed, and the gate
FAILS on it. That is the opposite of the connector-review gate, and
deliberate: this gate was fail-closed before the status existed, and a
release that skips e2e is worse than a re-run of a two-minute check.

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
E2E_LABEL = "e2e"

# Kept at module level so each line fits inside the tightest wrap in the
# fleet and `ruff format` is a no-op at every width (see FND-445).
MISSING_HINT = "Release PR needs a passing e2e run on its head. Add the 'e2e' label."
FAILED_HINT = "e2e failed on this commit. Fix it, then add the 'e2e' label again."
LABEL_OK = "Release PR has the 'e2e' label - Tests Gate enforces the result."


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
    """Return the ``e2e`` status state recorded in ``path``, else ``""``.

    ``gh api ... --jq`` prints one state per line; the combined-status
    endpoint already reduces each context to its newest status, so at most
    one line is expected. Take the last non-blank one regardless.
    """
    file = pathlib.Path(path)
    if not path or not file.is_file():
        return ""
    lines = [line.strip() for line in file.read_text().splitlines()]
    states = [line for line in lines if line]
    return states[-1] if states else ""


def decide(labels: list[str], e2e_state: str) -> tuple[bool, str]:
    """Return (passes, message) for a PR with ``labels``.

    ``e2e_state`` is the head commit's ``e2e`` status state, or ``""``
    when there is none or it could not be read.
    """
    if RELEASE_LABEL not in labels:
        return True, "Not a release PR - gate passes."
    if E2E_LABEL in labels:
        return True, LABEL_OK
    if e2e_state == "success":
        return True, "e2e passed on this commit - gate passes."
    if e2e_state in {"failure", "error"}:
        return False, FAILED_HINT
    return False, MISSING_HINT


def _build_parser() -> argparse.ArgumentParser:
    """Build the gate's argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels",
        default="[]",
        help="the PR's label names as a JSON array",
    )
    parser.add_argument(
        "--e2e-state-file",
        default="",
        help="file holding the head commit's `e2e` status state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the gate and return the process exit code."""
    args = _build_parser().parse_args(argv)
    passes, message = decide(
        parse_labels(args.labels),
        read_state(args.e2e_state_file),
    )
    if passes:
        print(message)  # noqa: T201
        return 0
    print(f"::error::{message}")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())

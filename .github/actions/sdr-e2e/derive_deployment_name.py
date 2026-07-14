"""Derive the per-leg ``ATLAN_DEPLOYMENT_NAME`` for the sdr-e2e composite action.

The worker container derives its Temporal task queue as
``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`` (see
``application_sdk.main._derive_task_queue``). When the e2e suite is fanned out
across parallel matrix legs (one job per ``tests/e2e/test_*.py`` file) every leg
shares the same GitHub run id — so without a per-leg discriminator all legs'
workers register on the *same* queue. Temporal then load-balances a single
workflow's activities across whichever leg-workers are co-serving that queue,
and under the two-store CI posture (per-container localstorage) an artifact
written by one leg's worker is invisible to the other → spurious
``[AAF-STR-001] FileReference ... resolved to no objects`` failures.

Appending the matrix leg name (the action's ``artifact-suffix``) to the
deployment name gives each leg its own queue + worker, restoring isolation. The
e2e harness derives its extract-node queue from the same two env vars
(``BaseE2ETest.agent_spec``), so worker and harness stay in lock-step with no
per-connector hard-coding.

Prints one ``ATLAN_DEPLOYMENT_NAME=<value>`` line suitable for
``>> "$GITHUB_ENV"``. Kept out of inline ``run:`` shell per
``docs/standards/ci.md`` (conditional logic belongs in tested Python).
"""

from __future__ import annotations

import argparse
import re

# The full-DAG CI deployment convention; matches BaseE2ETest.connection_name_prefix.
_BASE_PREFIX = "e2e-full-ci"
# Collapse anything outside a conservative charset to a single hyphen so the
# derived queue name stays log-friendly and stable across the worker + harness.
_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def derive(run_id: str, suffix: str) -> str:
    """Return ``e2e-full-ci-<run_id>[-<suffix>]``.

    *run_id* falls back to ``local`` when empty (developer / non-CI runs).
    *suffix* (the matrix leg name) is sanitised and appended only when set; a
    single-suite (non-matrix) run passes an empty suffix and keeps the original
    ``e2e-full-ci-<run_id>`` shape, so existing callers are unaffected.
    """
    run_token = run_id.strip() or "local"
    name = f"{_BASE_PREFIX}-{run_token}"
    leg = _SANITIZE.sub("-", suffix.strip()).strip("-").lower()
    if leg:
        name = f"{name}-{leg}"
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default="", help="GitHub run id (GITHUB_RUN_ID)."
    )
    parser.add_argument(
        "--suffix",
        default="",
        help="Matrix leg name (the action's artifact-suffix). Empty = single suite.",
    )
    args = parser.parse_args()
    print(f"ATLAN_DEPLOYMENT_NAME={derive(args.run_id, args.suffix)}")


if __name__ == "__main__":
    main()

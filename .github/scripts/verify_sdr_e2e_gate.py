#!/usr/bin/env python3
"""Fail Connector Tests Gate when the SDR K8s E2E leg did not pass (FND-4486).

``sdr-k8s-e2e`` used to be advisory everywhere: no ruleset context named it and
Connector Tests Gate did not need it, so a red SDR leg let a bump-version PR
into the merge queue and let the queue merge past it. The gate now needs the
job and calls this with both results.

Fail-closed, like the rest of the gate. ``sdr-k8s-e2e`` and ``connector-tests``
run on the same condition (the merge queue with sdk/container/ci changes, or a
PR whose head e2e was requested on); connector-tests additionally needs its
matrix and base image, which the upstream check has already required to be
healthy. So a skipped SDR leg is legitimate only when connector-tests skipped
too. A skipped SDR next to a dispatched connector run means the leg that should
have run did not, and that is a failure, never a pass.

Exits 1 with an ``::error::`` line on failure.

Kept out of the workflow YAML per docs/standards/ci.md and tested in
tests/test_verify_sdr_e2e_gate.py.
"""

from __future__ import annotations

import argparse
import sys

SDR_JOB_NAME = "SDR K8s E2E (LM)"


def evaluate(sdr_result: str, connector_result: str) -> str | None:
    """Return an error message, or None when the SDR leg does not block."""
    if sdr_result == "success":
        return None
    if sdr_result == "skipped":
        if connector_result == "skipped":
            return None
        return (
            f"{SDR_JOB_NAME} was skipped while the connector e2e ran "
            f"({connector_result}); the two run on the same condition, so "
            "failing the gate closed."
        )
    return (
        f"{SDR_JOB_NAME} did not succeed ({sdr_result or 'no result'}). "
        "Re-run the failed job; the gate re-runs with it."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Both required: a default would make a forgotten flag look like a skip.
    parser.add_argument("--sdr-result", required=True)
    parser.add_argument("--connector-result", required=True)
    args = parser.parse_args(argv)
    error = evaluate(args.sdr_result, args.connector_result)
    if error:
        print(f"::error::{error}")
        return 1
    print(f"{SDR_JOB_NAME}: {args.sdr_result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

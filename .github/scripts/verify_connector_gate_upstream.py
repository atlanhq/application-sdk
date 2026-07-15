#!/usr/bin/env python3
"""Verify the upstream jobs the connector e2e gate (Connector Tests Gate)
depends on, before deciding whether there's anything left to wait on.

Moved out of an inlined `run:` block per docs/standards/ci.md: this is a
chain of conditional checks, which belongs in a tested driver rather than
workflow YAML.

Fail-closed, mirroring SDK Gate: connector-tests is skipped both
legitimately (docs-only PR: sdk==false && container==false) AND on upstream
failure (matrix-builder failed, or the e2e-path base build failed). A naive
"skipped -> pass" would go green when ZERO connector tests ran because an
upstream job died. So changes + matrix-builder must have SUCCEEDED and
build-sdk-base-image must be success|skipped (never failure) before treating
a skipped connector-tests as the genuine docs-only skip.

connector-tests now only dispatches (e2e-apps wait-mode: callback) — its own
success means "dispatch succeeded", not "tests passed". The actual pass/fail
lands later via the check runs each connector completes directly
(tests-reusable.yaml's report-to-sdk job); "dispatched" here just tells the
caller whether there's anything to poll for.

Exits 1 (with an ::error:: line) if any upstream job failed. On success,
writes dispatched=true|false to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import os
import sys

OK_BASE_IMAGE_RESULTS = {"success", "skipped"}
OK_CONNECTOR_RESULTS = {"success", "skipped"}


def evaluate(
    changes_result: str,
    matrix_result: str,
    base_image_result: str,
    connector_result: str,
) -> tuple[str | None, bool]:
    """Returns (error_message, dispatched). error_message is None on success."""
    if changes_result != "success":
        return (
            f"Detect Changes did not succeed ({changes_result}) — failing the gate closed.",
            False,
        )
    if matrix_result != "success":
        return (
            f"Build Test Matrix did not succeed ({matrix_result}) — failing the gate closed.",
            False,
        )
    if base_image_result not in OK_BASE_IMAGE_RESULTS:
        return (
            f"Build SDK base image did not succeed ({base_image_result}) — failing the gate closed.",
            False,
        )
    if connector_result not in OK_CONNECTOR_RESULTS:
        return (
            f"Connector Tests dispatch did not succeed ({connector_result}) — failing the gate.",
            False,
        )
    return None, connector_result == "success"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changes-result", required=True)
    parser.add_argument("--matrix-result", required=True)
    parser.add_argument("--base-image-result", required=True)
    parser.add_argument("--connector-result", required=True)
    args = parser.parse_args(argv)

    error, dispatched = evaluate(
        args.changes_result,
        args.matrix_result,
        args.base_image_result,
        args.connector_result,
    )
    if error:
        print(f"::error::{error}")
        return 1

    if dispatched:
        print("Connector Tests dispatched — waiting on check runs.")
    else:
        print("✓ Connector Tests skipped (docs-only) — nothing to wait on.")

    line = f"dispatched={'true' if dispatched else 'false'}"
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

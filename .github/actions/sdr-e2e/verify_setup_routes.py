#!/usr/bin/env python3
"""CLI shell for the workflow-setup route check (FND-1667).

All the logic lives in :mod:`application_sdk.testing.setup_routes`, which is
pure and unit-tested offline. This file is the shell around it: argument
parsing, reading the token from the environment, flushing progress, and turning
its three outcomes into an exit code and a GitHub annotation.

Why the shell is here and the logic is in the SDK
-------------------------------------------------
The check asserts a join between what an app's contract generates and what its
tenant serves, and the SDK is on **both** sides of that join: the tenant's
``/api/service/configmaps/<name>`` is Heracles proxying to the app pod's own
``GET /workflows/v1/configmap/{id}`` in ``application_sdk.handler.service``.
So the envelope the check unwraps, the rule deciding which generated ``*.json``
is a setup form, and the bundle-vs-flat layout classification are all read from
``application_sdk.app.generated_tree`` — the same authority the server reads. A
copy of any of those in this directory would let the server serve one file while
the check compared against another, and that mismatch would read as a contract
regression rather than as two divergent copies of one rule.

Run with ``uv run python`` rather than ``python3``: it needs the app's synced
environment, which is where the SDK is importable. That is also why this step
lives in this composite — see the step's own comment in ``action.yaml``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from application_sdk.testing.setup_routes import (
    DEFAULT_CATALOG_WAIT_SECONDS,
    RouteCheckSkipped,
    SetupRouteError,
    TenantRoutes,
    verify,
)


def _bearer() -> str:
    """Read the tenant token from the environment, never from argv.

    ``ATLAN_API_KEY`` is what the e2e tenant resolver already exports for this
    leg, so nothing new has to be threaded through a GitHub expression.
    """
    for name in ("ATLAN_API_KEY", "E2E_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise SetupRouteError(
        "no tenant token: set ATLAN_API_KEY (the e2e tenant resolver already "
        "exports it for this leg). Read from the environment rather than passed "
        "on argv, which is visible in process listings and in `set -x` output."
    )


def _progress(message: str) -> None:
    """Print one poll-progress line, flushed.

    Flushed explicitly because Python block-buffers stdout when it is not a
    TTY, and a GitHub Actions step's ``run:`` is a pipe. Without the flush a
    poll loop's progress is invisible until the step exits — so a step that is
    patiently waiting looks identical to one that has hung, which is the
    diagnosis-hostile failure this whole check exists to avoid.
    """
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every entrypoint's workflow-setup page resolves on the tenant."
    )
    parser.add_argument("--base-url", required=True, help="https://<tenant>")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The app repo's working directory (holds atlan.yaml + app/generated).",
    )
    parser.add_argument(
        "--generated-dir",
        default="app/generated",
        help="Generated contract directory, relative to --repo-root.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_CATALOG_WAIT_SECONDS,
        help=(
            "How long to keep re-reading the marketplace catalog while it "
            "reconciles after the install. 0 reads once."
        ),
    )
    args = parser.parse_args(argv)

    try:
        lines = verify(
            Path(args.repo_root),
            TenantRoutes(base_url=args.base_url, bearer=_bearer()),
            generated_dir=args.generated_dir,
            wait_seconds=args.wait_seconds,
            on_progress=_progress,
        )
    except RouteCheckSkipped as exc:
        # Not a failure. An app with no generated contract, and one whose
        # entrypoints carry no marketplace card, both have no setup page to
        # check — and without this the check would be a fleet-wide false
        # positive on its first run. A notice rather than silence, so a skip is
        # visible in the log instead of looking like a pass.
        print(f"::notice::workflow-setup route check skipped: {exc}", flush=True)
        return 0
    except SetupRouteError as exc:
        print(f"::error::{exc}", file=sys.stderr, flush=True)
        return 1

    for line in lines:
        print(f"verified: {line}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

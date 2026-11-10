#!/usr/bin/env python3
"""CLI shell for the workflow-setup route check (FND-1667).

All the logic lives in :mod:`application_sdk.testing._setup_routes`, which is
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
``application_sdk.common._generated_tree`` — the same authority the server reads. A
copy of any of those in this directory would let the server serve one file while
the check compared against another, and that mismatch would read as a contract
regression rather than as two divergent copies of one rule.

Run with ``uv run python`` rather than ``python3``: it needs the app's synced
environment, which is where the SDK is importable. That is also why this step
lives in this composite — see the step's own comment in ``action.yaml``.

Version skew is the load-bearing detail here
--------------------------------------------
Both callers reference this action as ``@main``, so a change lands in every
repo's next e2e run at once. But the SDK it imports is the **connector repo's
own pinned version** — the harness is only repinned to a specific SDK ref on
cross-repo dispatch (``harness-sdk-ref``), and on an ordinary connector PR the
action's own comment says the harness "comes from the connector's OWN pinned
SDK".

So on the day this merges, the always-current action would be asking a
per-app-pinned SDK for a module that only exists from one release onwards.
Without the guard below that is a ``ModuleNotFoundError`` and a red e2e leg in
every connector still pinned below it — a fleet-wide break caused purely by
skew, with nothing wrong in any app.

The guard is an **import probe, not a version comparison**: it asks the exact
question that matters ("does the SDK on this runner carry the check?") rather
than a proxy that needs a floor constant kept in step with a release number.
The skew closes on its own as apps bump, with no second change here.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
from pathlib import Path

#: Set when the SDK on this runner predates the route check. Checked before the
#: import so an older pin skips instead of crashing — see the module docstring.
#:
#: ``find_spec`` returns ``None`` for a submodule that does not exist, and
#: raises ``ModuleNotFoundError`` when a PARENT is missing (no SDK synced at
#: all, which is not this check's problem and which every other step in the leg
#: will report). An ``ImportError`` from inside a module that DOES exist is
#: deliberately not caught here: that is a real defect in a new-enough SDK and
#: must surface as a failure rather than a skip.
#:
#: Two homes, newest first. FND-4660 moved the check to the private
#: ``_setup_routes`` and left ``setup_routes`` as a deprecated alias of it, so a
#: current SDK is read from the private module (no deprecation warning in every
#: e2e log), and an app pinned between FND-1667 and FND-4660 still finds the
#: public one. Same objects either way.
_CHECK_MODULES = (
    "application_sdk.testing._setup_routes",
    "application_sdk.testing.setup_routes",
)


def _find_check_module() -> str | None:
    """The first of :data:`_CHECK_MODULES` this runner's SDK carries, else None."""
    for name in _CHECK_MODULES:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except ModuleNotFoundError:
            return None
    return None


_CHECK_MODULE = _find_check_module()
_HAS_CHECK = _CHECK_MODULE is not None

if _CHECK_MODULE is not None:
    _check = importlib.import_module(_CHECK_MODULE)
    DEFAULT_CATALOG_WAIT_SECONDS = _check.DEFAULT_CATALOG_WAIT_SECONDS
    RouteCheckSkipped = _check.RouteCheckSkipped
    SetupRouteError = _check.SetupRouteError
    TenantRoutes = _check.TenantRoutes
    verify = _check.verify
else:  # pragma: no cover — exercised by the wiring test in a stripped env
    DEFAULT_CATALOG_WAIT_SECONDS = 120


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

    if not _HAS_CHECK:
        # Skew, not a failure. Named as such so a reader does not go looking for
        # a broken check: this app's pinned SDK simply predates it, and the skip
        # disappears when the pin moves.
        print(
            "::notice::workflow-setup route check skipped: this app's pinned "
            "atlan-application-sdk predates the check (application_sdk.testing."
            "setup_routes). "
            "The check ships with the SDK and runs automatically once this repo's "
            "SDK pin is bumped past the release that added it; nothing needs "
            "doing in this repo.",
            flush=True,
        )
        return 0

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

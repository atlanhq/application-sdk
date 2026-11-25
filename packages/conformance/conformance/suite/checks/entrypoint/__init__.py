"""P-series entrypoint-conformance checks — AST-based (BLDX-1411).

Enforces the contract that apps boot through the SDK's prescribed entrypoint
model rather than constructing workers, clients, or HTTP servers themselves.

* ``P017`` ManualWorkerBootstrap (app) — a call to ``create_worker(...)``,
  ``create_temporal_client(...)``, or ``AppWorker(...)`` whose binding resolves
  to ``application_sdk.execution``; an import of removed v2 boot surface
  (``application_sdk.worker``, ``application_sdk.application``,
  ``application_sdk.clients.temporal``); or a call to a distinctive v2
  lifecycle method (``.setup_workflow``, ``.start_workflow``, ``.start_worker``).
* ``P018`` ManualServerBootstrap (app) — a ``FastAPI(...)`` construction whose
  binding resolves to ``fastapi``; a ``uvicorn.run(...)`` call; or a call to a
  distinctive v2 server-lifecycle method (``.setup_server``, ``.start_server``,
  ``.include_router``).

Discovery note
--------------
Like the orchestration-seam rules (P004–P007), this series **includes test
files**.  An integration harness that hand-rolls a worker instead of using
``run_dev_combined`` is exactly the drift to catch, and it often lives in the
test tree.  The one carve-out: files under ``tests/integration/`` are exempt
from P017's construction-call and lifecycle-call violation classes, since
those harnesses need an in-process worker/client handle that
``run_dev_combined`` cannot provide — see ``_worker_bootstrap.py``.

Inline suppression
------------------
Add ``# conformance: ignore[P017] <reason>`` or ``[P018]`` on the offending
line (or the comment-only line directly above it).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover_with_tests,
    make_cli_main,
    make_scan_path,
)
from conformance.suite.schema.findings import Finding

from ._server_bootstrap import check_p018
from ._worker_bootstrap import check_p017

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover Python sources under *root*, **including** test files.

    Mirrors the orchestration-check discovery walk: skips infra/virtualenv/dot
    dirs but keeps the ``tests``/``test`` trees and ``test_*`` files — the
    entrypoint-conformance contract applies to test harnesses too.
    """
    return discover_with_tests(root)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P017 and P018 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p017(tree, file, directives),
        *check_p018(tree, file, directives),
    ]


scan_path = make_scan_path(scan_text)


main = make_cli_main(
    scan_text=scan_text,
    discover=discover,
    description="Entrypoint-conformance P-series checks (P017-P018): scan Python files.",
)


if __name__ == "__main__":
    sys.exit(main())

"""P-series entrypoint-conformance checks — AST-based (BLDX-1411).

Enforces the contract that apps boot through the SDK's prescribed entrypoint
model rather than constructing workers, clients, or HTTP servers themselves.

* ``P016`` ManualWorkerBootstrap (app) — a call to ``create_worker(...)``,
  ``create_temporal_client(...)``, or ``AppWorker(...)`` whose binding resolves
  to ``application_sdk.execution``; an import of removed v2 boot surface
  (``application_sdk.worker``, ``application_sdk.application``,
  ``application_sdk.clients.temporal``); or a call to a distinctive v2
  lifecycle method (``.setup_workflow``, ``.start_workflow``, ``.start_worker``).
* ``P017`` ManualServerBootstrap (app) — a ``FastAPI(...)`` construction whose
  binding resolves to ``fastapi``; a ``uvicorn.run(...)`` call; or a call to a
  distinctive v2 server-lifecycle method (``.setup_server``, ``.start_server``,
  ``.include_router``).

Discovery note
--------------
Like the orchestration-seam rules (P004–P007), this series **includes test
files**.  An integration harness that hand-rolls a worker instead of using
``run_dev_combined`` is exactly the drift to catch, and it often lives in the
test tree.

Inline suppression
------------------
Add ``# conformance: ignore[P016] <reason>`` or ``[P017]`` on the offending
line (or the comment-only line directly above it).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    EXCLUDE_DIRS,
    _parse_directives,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding

from ._server_bootstrap import check_p017
from ._worker_bootstrap import check_p016

SERIES = "P"

# This series scans test files too (see module docstring), so the universal
# ``tests``/``test`` directory exclusion is dropped from the discovery filter.
_EXCLUDE_DIRS = EXCLUDE_DIRS - {"tests", "test"}

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover Python sources under *root*, **including** test files.

    Mirrors the orchestration-check discovery walk: skips infra/virtualenv/dot
    dirs but keeps the ``tests``/``test`` trees and ``test_*`` files — the
    entrypoint-conformance contract applies to test harnesses too.
    """
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        if set(path.parts) & _EXCLUDE_DIRS:
            continue
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in rel_parts[:-1]):
            continue
        paths.append(path)
    return sorted(paths)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P016 and P017 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p016(tree, file, directives),
        *check_p017(tree, file, directives),
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P016 and P017 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text=scan_text,
    discover=discover,
    description="Entrypoint-conformance P-series checks (P016-P017): scan Python files.",
)


if __name__ == "__main__":
    sys.exit(main())

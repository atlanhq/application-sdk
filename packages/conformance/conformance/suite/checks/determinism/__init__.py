"""P-series determinism / async-correctness checks — AST-based.

Enforces that app and SDK code respects the SDK's async and determinism
expectations in the execution path, so subtle replay-correctness and event-loop
bugs are caught at CI time rather than under production orchestration.

* ``P020`` NonDeterministicPrimitiveInWorkflow — wall-clock time / uuid / sleep /
  randomness in a workflow-context method (``run`` / ``@entrypoint`` /
  ``@signal`` / ``@query`` / ``@update`` on an ``App`` subclass).
* ``P021`` SideEffectIoInWorkflow — file / network / env / thread-spawn I/O in a
  workflow-context method (belongs in a ``@task``).
* ``P022`` UnawaitedCoroutine — a bare ``self.<async-method>()`` statement that is
  never awaited (the call silently does nothing).
* ``P023`` BlockingCallInAsyncDef — an event-loop re-entry bridge
  (``asyncio.run`` / ``run_until_complete``) or a blocking sync I/O call inside an
  ``async def``.
* ``P024`` SyncAtlanClientInApp — pyatlan's synchronous ``AtlanClient`` used where
  the async ``AsyncAtlanClient`` (via the SDK credentials seam) is required.
* ``P031`` SharedDefaultExecutorOffload — ``asyncio.to_thread(...)`` or
  ``run_in_executor(None, ...)``, which land on asyncio's shared default executor
  instead of the SDK's dedicated ``run_in_thread()`` pool.

Discovery
---------
Unlike the orchestration P-series, this series uses the **standard** source
discovery walk — it deliberately **excludes** test files.  Workflow-determinism
mistakes in throwaway test fixtures are not production replay risks, and toy
``App`` subclasses in tests would only generate noise.

Scope
-----
All five rules are ``both``-scoped: workflow-context code and async SDK usage exist
in the SDK itself and in every consumer app, so the contract applies to any repo.

Inline suppression
------------------
Add ``# conformance: ignore[PXXX] <reason>`` on the offending line (or the
comment-only line directly above it).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding

from ._p020_primitives import check_p020
from ._p021_io import check_p021
from ._p022_unawaited import check_p022
from ._p023_blocking_async import check_p023
from ._p024_sync_atlan_client import check_p024
from ._p031_executor_offload import check_p031

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for all determinism findings (P020–P024, P031)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p020(tree, file, directives),
        *check_p021(tree, file, directives),
        *check_p022(tree, file, directives),
        *check_p023(tree, file, directives),
        *check_p024(tree, file, directives),
        *check_p031(tree, file, directives),
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P020–P024, P031 findings."""
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
    scan_all=lambda paths, root: [f for p in paths for f in scan_path(p, root)],
    description="Determinism / async-correctness P-series checks (P020-P024, P031).",
)


if __name__ == "__main__":
    sys.exit(main())

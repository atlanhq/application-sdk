"""K013 ManifestNodeAppNameMismatch / K014 ManifestNodeAppNameCollision.

Per-node ``app_name`` invariants over the committed generated DAG —
``app/generated/**/manifest.json`` or ``contract/generated/**/manifest.json``
(CNCT-129):

* **K013 (block):** within a node, ``app_name`` / ``inputs.app_name`` /
  ``inputs.args.app_name`` must agree (no-op when ``inputs.args.app_name`` is
  absent — not-yet-regenerated apps fall back to the env value).
* **K014 (warn):** no two distinct nodes in the same manifest may share an
  ``app_name`` (their logs would overlap in the tenant UI).

Both are **cross-artifact** checks that read generated JSON, not Python source,
so per-file ``scan_path`` is a no-op and ``scan_all`` does all the work —
mirrors ``manifest_contract`` (K006).
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

from ._check import scan_all

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: these rules need cross-artifact analysis; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K013/K014: per-node app_name consistency (block) and cross-node "
        "distinctness (warn) in app/generated/**/manifest.json (CNCT-129)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

"""K016 EntrypointArtifactSchemaMissing — undeclared artifact on a public boundary (ADR-0020).

Detects a ``FileReference`` field on an entry point's ``input``/``return``
contract — resolved across its full inheritance chain — that the app's
``artifactSchemas`` block does not describe.

The boundary needs no special-casing.  The default ``run()`` method is
registered as an *implicit* entry point carrying the same metadata as an
explicit ``@entrypoint``, so "every entry point's contracts" already means
"every public boundary"; internal ``@task`` contracts never become entry points
and are therefore exempt by construction rather than by a filter that could
drift.

The SDK reports the same defect at worker build as a deprecation warning
(``application_sdk/app/_artifact_schema_guard.py``), fatal in 4.0.  This rule
reports it in review instead, before a worker is ever built.

This is a **cross-file + cross-artifact** check: it scans all Python files to
build the entrypoint -> contract map, then reads the committed
``app/generated/`` tree.  Per-file scanning has no meaning here, so
``scan_path`` is a no-op and ``scan_all`` does all the work — mirrors K006
(``manifest_contract``), the check whose shape this one follows.
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
    """No-op: K016 requires cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K016 EntrypointArtifactSchemaMissing: verify every entry-point "
        "FileReference field has an artifactSchemas declaration (ADR-0020)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

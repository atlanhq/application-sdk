"""K017 ArtifactSchemaWriterMismatch — a declaration its writer has outgrown (ADR-0020).

Detects an ``artifactSchemas`` entry the app's own Python contradicts: a writer
that produces a file extension the declared ``format`` cannot be, or a record
class carrying a field the declaration does not describe.

K016 (``artifact_schema_declared``) catches "there is no declaration where one
is required".  K017 catches the next failure along: **the declaration exists and
disagrees with the writer**.  That is the drift class ADR-0020 exists for — the
declaration reads as a true statement about the file, so a consuming app trusts
it, and nothing in either app's tooling notices when the writer moves on without
it.  The SDK's runtime validation finds the same disagreement, but only once a
run has produced the artifact; this rule finds it in review, where it costs
nothing.

This is a **cross-file + cross-artifact** check: it reads the committed
``app/generated/**/artifact_schemas.json`` declarations, then scans all Python
files to build the class registry and resolve each declared field's writer.
Per-file scanning has no meaning here, so ``scan_path`` is a no-op and
``scan_all`` does all the work — mirrors K006 (``manifest_contract``) and K016,
the checks whose shape this one follows.
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
    """No-op: K017 requires cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K017 ArtifactSchemaWriterMismatch: verify a declared artifact schema "
        "agrees with the Python that writes the artifact (ADR-0020)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

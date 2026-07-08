"""K006 ManifestContractFieldMismatch — manifest-vs-contract field cross-check (BLDX-1527).

Detects a ``$.<node>.outputs.<field>`` JSONPath reference in the generated
``app/generated/**/manifest.json`` DAG that the referenced entrypoint's Python
``Output`` contract does not declare — directly or via an inherited base/mixin
(e.g. ``application_sdk.contracts.base.PublishInputMixin``).

This is a **cross-file + cross-artifact** check: it scans all Python files to
build the entrypoint -> Output contract map, then reads the committed
``app/generated/`` tree. Per-file scanning has no meaning here, so
``scan_path`` is a no-op and ``scan_all`` does all the work — mirrors P016
(``entrypoint_alignment``), the closest existing cross-artifact check.
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
    """No-op: K006 requires cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K006 ManifestContractFieldMismatch: verify manifest.json "
        "$.<node>.outputs.<field> refs against the Python Output contract (BLDX-1527)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

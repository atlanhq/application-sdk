"""P-series prescription checks — AST-based.

Scans Python source for above-the-bar prescription violations (P001–P099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Each rule lives in its own module (``_unbounded_fields`` → P001,
``_category_override`` → P002, …); this ``__init__`` assembles them into the
public scan + CLI API.  Adding a rule is a new module plus one line in
``scan_text``.

Currently implemented:

* ``P001`` UnboundedContractFields — an ``Input``/``Output`` contract subclass
  declared with the ``allow_unbounded_fields=True`` class keyword.
* ``P002`` CategoryFieldOverride — a subclass of ``AppError`` (or any of its
  14 categorical leaves) that redeclares the ``category`` ``ClassVar`` in its
  own body, drifting the canonical taxonomy.  The check uses a transitive
  closure of AppError-derived class names within the file so second-generation
  overrides are caught even when the intermediate class is not in the canonical
  set.

Inline suppression
------------------
Add a ``# conformance: ignore[P001] <reason>`` comment on the offending line or
the comment-only line immediately above it::

    # conformance: ignore[P001] generic cleanup payload — fields vary by app
    class StorageCleanupInput(Input, allow_unbounded_fields=True):
        ...
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

from ._category_override import (
    _CANONICAL_ERROR_CLASSES,
    CategoryOverrideChecker,
    _collect_apperror_subclasses,
)
from ._unbounded_fields import UnboundedContractFieldsChecker

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all P-series findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    directives = _parse_directives(text)

    p001 = UnboundedContractFieldsChecker(filename=file, directives=directives)
    p001.visit(tree)

    apperror_subclasses = _collect_apperror_subclasses(tree, _CANONICAL_ERROR_CLASSES)
    p002 = CategoryOverrideChecker(
        filename=file,
        directives=directives,
        apperror_subclasses=apperror_subclasses,
    )
    p002.visit(tree)

    return p001._findings + p002._findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
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
    scan_text,
    description="P-series: scan Python files for prescription violations.",
)
"""CLI entry point for P-series prescription checks."""


if __name__ == "__main__":
    sys.exit(main())

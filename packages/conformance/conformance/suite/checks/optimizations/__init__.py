"""O-series optimisation / recommendation checks — AST-based.

Scans Python source for below-the-prescription-bar recommendations (O001–O099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Each rule lives in its own module (``_stdlib_json`` → O001, …); this
``__init__`` assembles them into the public scan + CLI API.  Adding a rule is a
new module plus one line in ``scan_text``.

Currently implemented:

* ``O001`` OrjsonOverStdlibJson — ``json.dumps`` / ``json.loads`` call sites
  where ``json`` resolves to the stdlib module; prefer ``orjson``.

Inline suppression
------------------
Add a ``# conformance: ignore[O001] <reason>`` comment on the offending line or
the comment-only line immediately above it.

Known coverage limits (intentional — biased toward zero false positives at WARN
tier; documented rather than silently capped):

* **Out of scope by design:** ``json.dump`` / ``json.load`` (file-object APIs
  orjson does not provide), ``json.JSONDecodeError`` / ``JSONEncoder``, and
  other JSON libraries (``ujson``, ``simplejson``, ``rapidjson``) — O001 targets
  stdlib ``json`` only.
* **False negatives (not chased — same limitation the E-series accepts):**
  ``from json import *`` then bare ``dumps(...)``; rebinding ``j = json`` then
  ``j.dumps(...)``; storing the callable (``fn = json.dumps; fn(...)``).
* **Rare false positive:** a module-level ``import json`` *and* a function
  parameter also named ``json`` — a ``json.dumps`` inside that function is
  flagged even though it refers to the parameter.  WARN-tier and suppressible;
  proper scope resolution is deliberately not built for this edge.
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

from ._stdlib_json import OrjsonOverStdlibJsonChecker, _collect_json_bindings

SERIES = "O"

__all__ = [
    "SERIES",
    "_collect_json_bindings",
    "discover",
    "main",
    "scan_path",
    "scan_text",
]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all O-series findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    module_names, func_names = _collect_json_bindings(tree)
    directives = _parse_directives(text)
    checker = OrjsonOverStdlibJsonChecker(
        filename=file,
        directives=directives,
        json_module_names=module_names,
        json_func_names=func_names,
    )
    checker.visit(tree)
    return checker._findings


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
    description="O-series: scan Python files for optimisation/recommendation hits.",
)
"""CLI entry point for O-series optimisation checks."""


if __name__ == "__main__":
    sys.exit(main())

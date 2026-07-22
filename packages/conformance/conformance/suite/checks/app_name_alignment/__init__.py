"""P025 AppNameContractCodeDrift — app-name alignment check (BLDX-1491).

Detects misalignment between the app name derived from the App-family subclass in
code and the name declared in the committed contract artifacts (``atlan.yaml`` /
``app/generated/manifest.json``) or the ``ATLAN_APPLICATION_NAME`` env var in
``.env.example``.

This is a **cross-artifact** check: it scans all Python files to collect the
App-family leaf class and its derived name, then reads ``atlan.yaml`` and
``.env.example`` from the repo root.  Per-file scanning has no meaning here, so
``scan_path`` is a no-op and ``scan_all`` does all the work.

Currently implemented:

* ``P025`` AppNameContractCodeDrift — the code-derived app name (from the leaf
  App-family class's ``name = "..."`` or kebab-cased class name) must equal the
  ``atlan.yaml`` top-level ``name:`` and the ``.env.example``
  ``ATLAN_APPLICATION_NAME`` value when those artifacts are present.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    discover,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding

from ._check import check_p025
from ._code_app_name import (
    CodeAppNameScan,
    _RawClassDef,
    resolve_leaf_classes,
    scan_file,
)
from ._contract_app_name import scan_contract

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: P025 requires cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan *paths* for the App-family leaf class name and compare against contract artifacts.

    Called once by the runner with the full post-exclusion path list.

    Parameters
    ----------
    paths:
        Python source files to inspect (as returned by :func:`discover`).
    root:
        Repo root — used both for relative-URI construction in findings and for
        locating ``atlan.yaml`` and ``.env.example``.
    """
    raw: list[_RawClassDef] = []
    directives_by_file: dict[str, dict[int, _IgnoreDirective]] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        if not isinstance(tree, ast.Module):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel_str = str(rel)
        directives_by_file[rel_str] = _parse_directives(text)
        scan_file(tree, rel_str, raw)

    code: CodeAppNameScan = resolve_leaf_classes(raw)
    contract = scan_contract(root)
    return check_p025(code, contract, directives_by_file)


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "P025 AppNameContractCodeDrift: verify that the App-family class name "
        "matches atlan.yaml name: and .env.example ATLAN_APPLICATION_NAME (BLDX-1491)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

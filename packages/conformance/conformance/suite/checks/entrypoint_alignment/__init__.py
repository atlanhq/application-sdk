"""P016 EntryPointContractCodeDrift — entry-point alignment check (BLDX-1425).

Detects misalignment between the entry-point names declared in the Pkl contract
(``app/generated/<name>/manifest.json`` dirs) and those registered in code
(``@entrypoint``-decorated ``App`` methods).

This is a **cross-file + cross-artifact** check: it scans all Python files to
collect ``@entrypoint`` wire names, then inspects the ``app/generated/`` tree
from the repo root.  Per-file scanning has no meaning here, so ``scan_path``
is a no-op and ``scan_all`` does all the work.

Currently implemented:

* ``P016`` EntryPointContractCodeDrift — multi-EP apps: ``@entrypoint`` wire
  names must exactly equal ``app/generated/<name>/`` subdir names; single-EP
  apps: at most one ``@entrypoint`` allowed; non-literal ``name=`` flagged as
  unverifiable.
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

from ._check import check_p016
from ._code_entrypoints import CodeEntrypointScan, scan_file_for_entrypoints
from ._contract_entrypoints import scan_contract

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: P016 requires cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan *paths* for ``@entrypoint`` names and compare against ``app/generated/``.

    Called once by the runner with the full post-exclusion path list.

    Parameters
    ----------
    paths:
        Python source files to inspect (as returned by :func:`discover`).
    root:
        Repo root — used both for relative-URI construction in findings and for
        locating the ``app/generated/`` directory.
    """
    code = CodeEntrypointScan()
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
        scan_file_for_entrypoints(tree, rel_str, code)

    contract = scan_contract(root)
    return check_p016(code, contract, directives_by_file)


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "P016 EntryPointContractCodeDrift: verify that @entrypoint wire names "
        "match app/generated/ contract dirs (BLDX-1425)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())

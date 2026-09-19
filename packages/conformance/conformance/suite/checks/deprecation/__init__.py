"""B-series deprecation checks — AST-based, mixed-scope.

One checker, two halves plus a contract-compat pass, dispatched by scope:

* **consumer half (B001/B007/B008, scope ``app``)** — B001 flags app usage of any
  SDK symbol the committed manifest marks deprecated; B007 flags daft-only
  DataFrame APIs (``count_rows``/``to_pylist``/``.names``,
  ``DataframeType.daft``) that are dead on the daft-less SDK >= 3.22 runtime —
  third-party surfaces the generated manifest cannot carry; B008 flags an import
  or attribute use that reaches an ``_``-prefixed module or name the app does
  not own — the SDK's, or any other package's — which no manifest can carry
  either, because a private symbol is never deprecated before it changes
  (FND-2388);
* **authoring half (B002/B003/B004, scope ``sdk``)** — flags the SDK declaring
  its own deprecations incorrectly (malformed notice, overdue removal, or an
  unmarked docstring claim);
* **contract-compat (B005/B006, scope ``both``)** — guards entrypoint contracts
  against non-additive changes (field removal, type change) and ledger staleness.

Scope is detected once per run from the repo's ``[project].name`` (the same
mechanism every series uses): the SDK runs only the authoring half, a consumer
app runs only the consumer half, and an undetectable repo runs both.  The runner
additionally post-filters findings by each rule's declared scope, so correctness
never depends on this dispatch alone.

Inline suppression
------------------
Add ``# conformance: ignore[B001] <reason>`` (or B002–B008) on the offending
line or the comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    detect_scope,
    discover,
    make_cli_main,
)
from conformance.suite.schema.disposition import RuleScope
from conformance.suite.schema.findings import Finding

from ._authoring import scan_authoring
from ._consumer import scan_consumer
from ._contract_compat import scan_contract_compat
from ._daft_runtime import scan_daft_runtime
from ._ledger_schema import load_ledger
from ._manifest import load_manifest
from ._private_imports import own_import_roots, scan_private_imports

SERIES = "B"

__all__ = [
    "SERIES",
    "discover",
    "load_ledger",
    "main",
    "scan_all",
    "scan_authoring",
    "scan_consumer",
    "scan_contract_compat",
    "scan_daft_runtime",
    "scan_path",
    "scan_private_imports",
]


def _read_project_version(root: Path) -> str | None:
    """Return ``[project].version`` from ``<root>/pyproject.toml``, else ``None``."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if isinstance(project, dict) and isinstance(project.get("version"), str):
        return project["version"]
    return None


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan *paths* and return all in-scope B-series findings.

    Resolves consumer vs authoring once from *root*, loads the manifest / SDK
    version once, then runs the relevant half over every file.
    """
    scope = detect_scope(root)
    run_consumer = scope in (RuleScope.APP, None)
    run_authoring = scope in (RuleScope.SDK, None)

    manifest = load_manifest() if run_consumer else None
    version = _read_project_version(root) if run_authoring else None
    # Resolved once per run: B008 needs to tell the app's own privates (fine)
    # from everybody else's (not fine), and that is a property of the repo, not
    # of any one file.
    own_roots = own_import_roots(root) if run_consumer else frozenset()

    findings: list[Finding] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        directives = _parse_directives(text)
        if run_consumer and manifest is not None:
            findings.extend(scan_consumer(tree, rel, manifest, directives))
        if run_consumer:
            findings.extend(scan_daft_runtime(tree, rel, directives))
            findings.extend(scan_private_imports(tree, rel, directives, own_roots))
        if run_authoring:
            findings.extend(scan_authoring(tree, rel, version, directives))

    ledger = load_ledger(repo_root=root)
    findings.extend(scan_contract_compat(paths, root, ledger, scope))
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single file (delegates to :func:`scan_all`)."""
    return scan_all([path], root)


main = make_cli_main(
    scan_all=scan_all,
    description="B-series: scan for deprecated-symbol usage and authoring hygiene.",
)
"""CLI entry point for B-series deprecation checks."""


if __name__ == "__main__":
    sys.exit(main())

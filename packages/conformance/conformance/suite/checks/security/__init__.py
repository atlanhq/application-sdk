"""S-series security / secret-hygiene checks — AST-based.

Scans Python source for secret-handling drift (BLDX-1419):

* ``S001`` HardcodedCredential — a non-empty string literal stored as a
  credential-named variable, keyword argument, or dict value.
* ``S002`` RawEnvCredentialAccess — a credential-named environment variable read
  directly via ``os.getenv`` / ``os.environ`` instead of the SDK secret store
  (app-scoped — the runner skips it on the SDK).

The issue's "no credentials in logs" clause is enforced by the existing
``L010 CredentialInLogOutput`` rule, not restated here.

Every check is purely deterministic: the same source text always produces the
same findings.  Both rules are per-file (no cross-file pass).

Discovery extends the shared Python-source walk with two security-specific
exclusions — ``run_dev*.py`` and ``scripts/`` — which are local dev harnesses
that legitimately read secret-named env to seed mock stores; without them S002
would fire on ~90% of the fleet, including the reference app.

Inline suppression
------------------
Add a ``# conformance: ignore[SXXX] <reason>`` comment on the offending line or
the comment-only line immediately above it::

    # conformance: ignore[S002] platform self-auth — no SDK secret-store seam
    token = os.getenv("ATLAN_API_KEY")
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks._ast_common import discover as _shared_discover
from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.schema.findings import Finding

from ._s001 import HardcodedCredentialChecker
from ._s002 import check_s002

SERIES = "S"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]

# Dev-harness exclusions on top of the shared walk (tests/ and .github/ are
# already dropped there).  ``run_dev*.py`` and anything under ``scripts/`` seed
# local mock secret stores from env and are not shipped application code.
_HARNESS_DIRS: frozenset[str] = frozenset({"scripts"})


def _is_harness(rel: Path) -> bool:
    if rel.name.startswith("run_dev"):
        return True
    return bool(set(rel.parts) & _HARNESS_DIRS)


def discover(root: Path) -> list[Path]:
    """Discover Python sources, excluding dev harnesses (``run_dev*.py``, ``scripts/``)."""
    return [p for p in _shared_discover(root) if not _is_harness(p.relative_to(root))]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for S001 + S002 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    directives = _parse_directives(text)

    s001 = HardcodedCredentialChecker(filename=file, directives=directives)
    s001.visit(tree)

    return s001._findings + check_s002(tree, file, directives)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for S001 + S002 findings."""
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
    description="S-series: scan Python files for secret-hygiene violations.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())

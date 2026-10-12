"""P-series client-lifecycle check — AST-based (FND-3778).

Enforces that ``BaseClient`` subclasses release their shared httpx connection
pool:

* ``P056`` UnclosedBaseClient (app) — a function binds a subclass instance to a
  local name and neither enters it with ``async with`` nor calls
  ``await <name>.close()`` in a ``finally``.
* ``P057`` CloseOverrideSkipsSuper (app) — a subclass overrides ``close()`` and
  never calls ``super().close()``.

Registered under series letter ``P`` alongside ``prescriptions``,
``client_seam`` and the other seam checks, the established multi-module pattern.

Cross-file resolution
---------------------
The subclass and the code that constructs it usually live in different files
(``app/client.py`` / ``app/handler.py``), so :func:`scan_all` parses every
discovered file once, resolves the subclass registry over all of them, then
runs both rules per file.  :func:`scan_text` resolves within the one file it is
given.

SDK-version gate
----------------
Before application-sdk 3.46.0 ``BaseClient`` built an ``httpx.AsyncClient`` per
request, so its pool was dropped after every call, an unclosed client leaked
nothing, and ``async with`` was not supported.  :func:`scan_all` and
:func:`scan_path` read the SDK version the repo's ``uv.lock`` resolves (the
same reader ``P051`` / ``P053`` use) and return nothing unless it is confirmed at
or above :data:`SHARED_POOL_SDK_FLOOR` — no lock, no SDK in it, or an
unparseable version all stay silent.  :func:`scan_text` is the pure AST pass
with no repo to read, so it is ungated.

Inline suppression
------------------
Add ``# conformance: ignore[P056] <reason>`` (or ``P057``) on the offending line
(or the comment-only line directly above it).
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
from conformance.suite.checks._ast_common._imports import collect_import_origins
from conformance.suite.checks._version import (
    locked_sdk_version,
    parse_version,
    version_reached,
)
from conformance.suite.schema.findings import Finding

from ._unclosed_client import (
    ClassRecord,
    check_p056,
    check_p057,
    collect_class_bases,
    resolve_subclasses,
)

SERIES = "P"

#: First application-sdk release whose ``BaseClient`` keeps one shared pool open
#: until ``close()`` (application-sdk#4309).  Silent below it.
SHARED_POOL_SDK_FLOOR = (3, 46, 0)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


def sdk_has_shared_pool(root: Path) -> bool:
    """True iff ``root/uv.lock`` resolves an SDK whose ``BaseClient`` shares a pool.

    Fails closed — an unreadable version is not evidence the client can leak,
    and prescribing ``async with`` to an SDK that does not support it is a
    finding nobody can act on.
    """
    locked = locked_sdk_version(root)
    if locked is None:
        return False
    parsed = parse_version(locked)
    return parsed is not None and version_reached(SHARED_POOL_SDK_FLOOR, parsed)


class _ParsedFile:
    """One parsed source file and what both rules need from it."""

    __slots__ = ("directives", "origins", "records", "rel", "tree")

    def __init__(self, text: str, rel: str) -> None:
        self.rel = rel
        self.tree: ast.AST = ast.parse(text, filename=rel)
        self.directives: dict[int, _IgnoreDirective] = _parse_directives(text)
        self.origins = collect_import_origins(self.tree)
        self.records: list[ClassRecord] = collect_class_bases(self.tree, self.origins)


def _scan_parsed(files: list[_ParsedFile]) -> list[Finding]:
    subclasses = resolve_subclasses(r for f in files for r in f.records)
    if not subclasses:
        return []
    findings: list[Finding] = []
    for f in files:
        findings.extend(check_p057(f.records, subclasses, f.rel, f.directives))
        findings.extend(check_p056(f.tree, subclasses, f.rel, f.directives, f.origins))
    return findings


def _parse_file(path: Path, root: Path) -> _ParsedFile | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    try:
        return _ParsedFile(text, str(rel))
    except SyntaxError:
        return None


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one source *text* for P056/P057, resolving subclasses in-file (ungated)."""
    try:
        parsed = _ParsedFile(text, file)
    except SyntaxError:
        return []
    return _scan_parsed([parsed])


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan one Python file, gated on *root*'s locked SDK version."""
    if not sdk_has_shared_pool(root):
        return []
    parsed = _parse_file(path, root)
    return _scan_parsed([parsed]) if parsed is not None else []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan every discovered file with a repo-wide subclass registry (gated)."""
    if not sdk_has_shared_pool(root):
        return []
    parsed = [p for p in (_parse_file(path, root) for path in paths) if p is not None]
    return _scan_parsed(parsed)


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Client-lifecycle P-series check (P056/P057): scan Python files for "
        "BaseClient subclasses that are never closed, and close() overrides that "
        "skip super().close() (silent below application-sdk 3.46.0)."
    ),
)
"""CLI entry point for the client-lifecycle check."""


if __name__ == "__main__":
    sys.exit(main())

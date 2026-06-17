"""P-series prescription checks — AST-based.

Scans Python source for above-the-bar prescription violations (P001–P099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Each rule lives in its own module (``_unbounded_fields`` → P001,
``_category_override`` → P002, ``_error_code_prefix`` → P003, …); this
``__init__`` assembles them into the public scan + CLI API.  Adding a rule is
a new module plus one line in ``scan_text`` (for per-file rules) or a new
pass in ``scan_all`` (for cross-file rules).

Currently implemented:

* ``P001`` UnboundedContractFields — an ``Input``/``Output`` contract subclass
  declared with the ``allow_unbounded_fields=True`` class keyword.
* ``P002`` CategoryFieldOverride — a subclass of ``AppError`` (or any of its
  14 categorical leaves) that redeclares the ``category`` ``ClassVar`` in its
  own body, drifting the canonical taxonomy.  The check uses a transitive
  closure of AppError-derived class names within the file so second-generation
  overrides are caught even when the intermediate class is not in the canonical
  set.
* ``P003`` ErrorCodePrefixMismatch — a (transitive) subclass of an
  ``application_sdk.errors`` leaf class either omits its own ``code: ClassVar[str]``
  declaration or declares one that does not start with the leaf's category prefix.
  This check resolves inheritance across files via a multi-pass scan, so
  intermediate pass-through classes are flagged too.

Inline suppression
------------------
Add a ``# conformance: ignore[PXXX] <reason>`` comment on the offending line or
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
    _IgnoreDirective,
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
from ._error_code_prefix import (
    LEAF_PREFIX_MAP,
    ClassRecord,
    collect_classes,
    collect_import_aliases,
    emit_p003,
    resolve_leaf_prefix,
)
from ._unbounded_fields import UnboundedContractFieldsChecker

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P001 and P002 findings.

    P003 needs cross-file context; use :func:`scan_all` for full-suite runs.
    Kept for symmetry with the per-file ``scan_path`` runner contract.
    """
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
    """Scan a single Python file (P001 + P002 only).  P003 requires :func:`scan_all`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Multi-pass scan over *paths*, emitting P001 + P002 + P003 findings.

    Pass 1 — parse every file once, run P001 and P002 per-file, collect every
    ``ClassDef`` into a name-keyed registry along with its base names and any
    literal ``code`` declaration.

    Pass 2 — for each registered class, walk its (transitive) base chain to
    find the deepest ancestor that names one of the 14 leaves in
    ``LEAF_PREFIX_MAP``.  Cycle-safe via a per-resolution ``visiting`` set;
    results memoised.

    Pass 3 — emit P003 for every class that derives from a leaf and either
    omits its own ``code`` declaration or declares one that does not start
    with the leaf's category prefix + ``_``.  The 14 leaves themselves are
    exempt (their bare codes are the prefix definitions).
    """
    findings: list[Finding] = []

    # Pass 1
    file_directives: dict[Path, dict[int, _IgnoreDirective]] = {}
    file_records: dict[Path, list[ClassRecord]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel_str = str(rel)
        directives = _parse_directives(text)
        file_directives[path] = directives

        # P001 (per-file)
        checker = UnboundedContractFieldsChecker(
            filename=rel_str, directives=directives
        )
        checker.visit(tree)
        findings.extend(checker._findings)

        # P002 (per-file)
        apperror_subclasses = _collect_apperror_subclasses(
            tree, _CANONICAL_ERROR_CLASSES
        )
        p002_checker = CategoryOverrideChecker(
            filename=rel_str,
            directives=directives,
            apperror_subclasses=apperror_subclasses,
        )
        p002_checker.visit(tree)
        findings.extend(p002_checker._findings)

        # Class registry for P003 — de-alias base names so aliased imports
        # of leaf classes don't hide the real ancestry.
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        records = collect_classes(tree, rel_str, aliases)
        file_records[path] = records
        for rec in records:
            by_name.setdefault(rec.name, rec)

    # Pass 2 + 3 — resolve and emit P003
    cache: dict[str, str | None] = {}
    for path, records in file_records.items():
        directives = file_directives[path]
        for rec in records:
            if rec.name in LEAF_PREFIX_MAP:
                continue
            leaf_prefix: str | None = None
            for base in rec.bases:
                leaf_prefix = resolve_leaf_prefix(base, by_name, cache, set())
                if leaf_prefix is not None:
                    break
            if leaf_prefix is None:
                continue
            if rec.code_value is None:
                findings.append(emit_p003(rec, leaf_prefix, directives))
            elif not rec.code_value.startswith(f"{leaf_prefix}_"):
                findings.append(emit_p003(rec, leaf_prefix, directives))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description="P-series: scan Python files for prescription violations.",
)


if __name__ == "__main__":
    sys.exit(main())

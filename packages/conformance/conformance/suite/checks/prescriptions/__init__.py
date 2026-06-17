"""P-series prescription checks — AST-based.

Scans Python source for above-the-bar prescription violations (P001–P099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Currently implemented:

* ``P001`` UnboundedContractFields — an ``Input``/``Output`` contract subclass
  declared with the ``allow_unbounded_fields=True`` class keyword.
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

import argparse
import ast
import json
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    TOOL_VERSION,
    _IgnoreDirective,
    _parse_directives,
    discover,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding, findings_to_report

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
    """Scan a single Python source *text* for P001 findings only.

    P003 needs cross-file context; use :func:`scan_all` for full-suite runs.
    Kept for symmetry with the per-file ``scan_path`` runner contract.
    """
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    directives = _parse_directives(text)
    checker = UnboundedContractFieldsChecker(filename=file, directives=directives)
    checker.visit(tree)
    return checker._findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file (P001 only).  P003 requires :func:`scan_all`."""
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
    """Multi-pass scan over *paths*, emitting P001 + P003 findings.

    Pass 1 — parse every file once, run P001 per-file, collect every ``ClassDef``
    into a name-keyed registry along with its base names and any literal
    ``code`` declaration.

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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for P-series prescription checks."""
    parser = argparse.ArgumentParser(
        description="P-series: scan Python files for prescription violations."
    )
    parser.add_argument(
        "scan_paths",
        nargs="*",
        default=["."],
        metavar="PATH",
        help="Directories or files to scan (default: .)",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="Repo root for relative URI construction (default: .)",
    )
    parser.add_argument(
        "--sarif-output",
        metavar="FILE",
        help="Write SARIF report to FILE (default: stdout)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate emitted SARIF against the official schema",
    )
    parser.add_argument("--tool-version", default=TOOL_VERSION, metavar="VERSION")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    # Resolve every input to a flat path list so scan_all can build a single
    # cross-file class registry — P003 requires this.
    collected: list[Path] = []
    for raw in args.scan_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            collected.append(p)
        elif p.is_dir():
            collected.extend(discover(p))

    findings = scan_all(collected, root)

    report = findings_to_report(findings, tool_version=args.tool_version)

    if args.validate:
        from conformance.suite.schema.validate import validate_sarif

        validate_sarif(report)

    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)
    if args.sarif_output:
        Path(args.sarif_output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


# make_cli_main is the per-file CLI helper; keep it available for completeness
# but main() above supersedes it for P-series (which needs cross-file scan_all).
_ = make_cli_main  # suppress unused-import linter noise


if __name__ == "__main__":
    sys.exit(main())

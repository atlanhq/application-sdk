"""P-series prescription checks — AST-based.

Scans Python source for above-the-bar prescription violations (P001–P099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Currently implemented:

* ``P001`` UnboundedContractFields — an ``Input``/``Output`` contract subclass
  declared with the ``allow_unbounded_fields=True`` class keyword.
* ``P002`` CategoryFieldOverride — a subclass of ``AppError`` (or any of its
  14 categorical leaves) that redeclares the ``category`` ``ClassVar`` in its
  own body, drifting the canonical taxonomy.

Inline suppression
------------------
Add a ``# conformance: ignore[P001] <reason>`` comment on the offending line or
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

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks.error_handling import discover
from conformance.suite.checks.error_handling._constants import LEAF_CLASSES
from conformance.suite.checks.error_handling._directives import (
    _IgnoreDirective,
    _parse_directives,
)
from conformance.suite.checks.error_handling._helpers import _get_name
from conformance.suite.schema.findings import Finding, findings_to_report

SERIES = "P"

# P002 — names of the canonical SDK error classes (the ``AppError`` base plus
# the 14 categorical leaves in ``application_sdk/errors/leaves.py``).  These
# are the sole defining sites for ``FailureCategory``: any class outside this
# set that subclasses one of them and assigns ``category`` in its body is a
# taxonomy override.  Matched on the *simple* base name so both
# ``class Foo(NotFoundError):`` and ``class Foo(errors.NotFoundError):`` are
# covered.
_CANONICAL_ERROR_CLASSES: frozenset[str] = LEAF_CLASSES | {"AppError"}


def _find_category_assignment(cls: ast.ClassDef) -> ast.stmt | None:
    """Return the first class-body statement assigning to a top-level ``category``.

    Matches ``category: ClassVar[FailureCategory] = ...`` (``AnnAssign`` whose
    target is the simple Name ``category``) and ``category = ...`` (``Assign``
    targeting Name ``category``).  Annotation-only forms with no value
    (``category: ClassVar[FailureCategory]``) are not redeclarations — they
    refine the type without binding a value — and are not flagged.
    """
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id == "category"
                and stmt.value is not None
            ):
                return stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "category":
                    return stmt
    return None


class _PrescriptionChecker(ast.NodeVisitor):
    """Walk a module AST and emit P-series findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._findings: list[Finding] = []

    # ── P001 ──────────────────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for kw in node.keywords:
            if kw.arg != "allow_unbounded_fields":
                continue
            # The opt-out is active for ANY truthy value: Input/Output's
            # __init_subclass__ does ``if allow_unbounded_fields:``.  So
            # ``=True``, ``=1`` and dynamic values (``=FLAG``, ``=(expr)``) all
            # opt out.  Only an explicit literal-falsy value (False/None/0/"")
            # is a genuine opt-back-in and must NOT be flagged.
            if isinstance(kw.value, ast.Constant) and not kw.value.value:
                break
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=(
                        f"Contract '{node.name}' opts out of payload-safety "
                        "enforcement via allow_unbounded_fields — arbitrary untyped "
                        "fields may cross task boundaries. This must be exceptional: "
                        "justify it with an inline '# conformance: ignore[P001] "
                        "<reason>' directive at the declaration site (and prefer a "
                        "non-dynamic value so the opt-out is statically auditable)."
                    ),
                    directives=self._directives,
                )
            )
            break

        # ── P002 ──────────────────────────────────────────────────────────────
        # The canonical SDK error classes themselves are the *defining sites*
        # for ``category`` — exempt them from the rule by name.  Any other
        # class that subclasses one of them and assigns ``category`` in its
        # body is overriding the canonical taxonomy.
        if node.name not in _CANONICAL_ERROR_CLASSES and any(
            _get_name(base) in _CANONICAL_ERROR_CLASSES for base in node.bases
        ):
            assign_node = _find_category_assignment(node)
            if assign_node is not None:
                self._findings.append(
                    make_finding(
                        filename=self._filename,
                        rule_id="P002",
                        node=assign_node,
                        message=(
                            f"Class '{node.name}' redeclares the `category` "
                            "ClassVar — drifts the canonical FailureCategory "
                            "taxonomy. Domain subclasses must inherit `category` "
                            "from their categorical-leaf parent and specialise via "
                            "`code` (and evidence fields) only. If the redeclaration "
                            "is genuinely necessary, justify it with an inline "
                            "'# conformance: ignore[P002] <reason>' directive at the "
                            "assignment site."
                        ),
                        directives=self._directives,
                    )
                )

        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public check API
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all P-series findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    directives = _parse_directives(text)
    checker = _PrescriptionChecker(filename=file, directives=directives)
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
    parser.add_argument("--tool-version", default="0.2.0", metavar="VERSION")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings: list[Finding] = []
    for raw in args.scan_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            findings.extend(scan_text(p.read_text(encoding="utf-8"), str(rel)))
        elif p.is_dir():
            for py_file in discover(p):
                findings.extend(scan_path(py_file, root))

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


if __name__ == "__main__":
    sys.exit(main())

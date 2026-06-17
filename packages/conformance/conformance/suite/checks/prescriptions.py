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
from dataclasses import dataclass, field
from pathlib import Path

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks.error_handling import discover
from conformance.suite.checks.error_handling._directives import (
    _IgnoreDirective,
    _parse_directives,
)
from conformance.suite.checks.error_handling._helpers import _get_name
from conformance.suite.schema.findings import Finding, findings_to_report

SERIES = "P"

# ── P003: leaf-class → category prefix map ───────────────────────────────────
# Mirrors application_sdk/errors/leaves.py.  Subclasses (transitively) of these
# 14 classes must declare a ``code: ClassVar[str]`` that starts with the
# corresponding prefix followed by an underscore.
_LEAF_PREFIX_MAP: dict[str, str] = {
    "CancelledError": "CANCELLED",
    "AppTimeoutError": "TIMEOUT",
    "RateLimitedError": "RATE_LIMITED",
    "AuthError": "AUTH",
    "AppPermissionDeniedError": "PERMISSION",
    "NotFoundError": "NOT_FOUND",
    "AlreadyExistsError": "ALREADY_EXISTS",
    "InvalidInputError": "INVALID_INPUT",
    "PreconditionError": "PRECONDITION",
    "DependencyUnavailableError": "DEPENDENCY_UNAVAILABLE",
    "ResourceExhaustedError": "RESOURCE_EXHAUSTED",
    "DataIntegrityError": "DATA_INTEGRITY",
    "InternalError": "INTERNAL",
    "UnimplementedError": "UNIMPLEMENTED",
}


# ── P001: per-file checker ────────────────────────────────────────────────────


class _PrescriptionChecker(ast.NodeVisitor):
    """Walk a module AST and emit P-series findings that don't need cross-file context."""

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
        self.generic_visit(node)


# ── P003: cross-file class registry ───────────────────────────────────────────


@dataclass
class _ClassRecord:
    """A ClassDef collected for P003 inheritance resolution."""

    name: str
    file: str
    node: ast.ClassDef
    bases: list[str] = field(default_factory=list)
    code_value: str | None = None  # None when no ``code: ClassVar[str] = "..."``
    code_node: ast.AST | None = None  # AnnAssign/Assign node (or None)


def _is_classvar_annotation(annotation: ast.expr | None) -> bool:
    """True if ``annotation`` is ``ClassVar`` or ``ClassVar[...]`` (any form)."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Subscript):
        return _get_name(annotation.value) == "ClassVar"
    return _get_name(annotation) == "ClassVar"


def _extract_code(cls_node: ast.ClassDef) -> tuple[str | None, ast.AST | None]:
    """Find the class-level ``code`` literal assignment, if any.

    Accepts both ``code: ClassVar[str] = "..."`` (the prescribed form) and the
    plain ``code = "..."`` shorthand — both bind a class attribute and the rule
    cares about the literal string value either way.
    """
    for stmt in cls_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id != "code":
                continue
            if not _is_classvar_annotation(stmt.annotation):
                continue
            if (
                stmt.value is not None
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                return stmt.value.value, stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "code"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    return stmt.value.value, stmt
    return None, None


def _collect_import_aliases(tree: ast.Module) -> dict[str, str]:
    """Return per-file ``{local_name: original_name}`` for ``from X import Y as Z`` /
    ``from X import Y`` statements.

    Maps local identifiers to the original imported name so that a base
    reference like ``class AppContextError(_InternalError):`` resolves to
    ``InternalError`` (its real leaf class) instead of being a false-negative
    when the file imports ``InternalError as _InternalError``.

    Plain ``import X`` and ``import X as Y`` are not relevant to leaf
    resolution (the base would have to be written as ``X.LeafError`` and
    ``_get_name`` already extracts the rightmost attribute), so they are
    skipped here.
    """
    aliases: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            local = alias.asname if alias.asname else alias.name
            aliases[local] = alias.name
    return aliases


def _collect_classes(
    tree: ast.AST, rel_file: str, aliases: dict[str, str]
) -> list[_ClassRecord]:
    """Walk *tree* and return one record per top-level/nested ClassDef.

    Base names are de-aliased through *aliases* so a class declared with an
    aliased import (``class X(_InternalError):`` after
    ``from … import InternalError as _InternalError``) is recorded with the
    real leaf name in its bases list — without this, alias-imported leaves
    would be silently invisible to the resolver and produce false-negatives.
    """
    records: list[_ClassRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases: list[str] = []
        for base in node.bases:
            n = _get_name(base)
            if n is None:
                continue
            bases.append(aliases.get(n, n))
        code_value, code_node = _extract_code(node)
        records.append(
            _ClassRecord(
                name=node.name,
                file=rel_file,
                node=node,
                bases=bases,
                code_value=code_value,
                code_node=code_node,
            )
        )
    return records


def _resolve_leaf_prefix(
    name: str,
    by_name: dict[str, _ClassRecord],
    cache: dict[str, str | None],
    visiting: set[str],
) -> str | None:
    """Walk the (transitive) base chain of *name* and return the leaf prefix.

    Returns ``None`` if *name* is not derived (transitively) from one of the 14
    leaves in ``_LEAF_PREFIX_MAP``.  Cycle-safe via ``visiting``.
    """
    if name in _LEAF_PREFIX_MAP:
        return _LEAF_PREFIX_MAP[name]
    if name in cache:
        return cache[name]
    if name in visiting:
        return None
    rec = by_name.get(name)
    if rec is None:
        cache[name] = None
        return None
    visiting.add(name)
    result: str | None = None
    for base in rec.bases:
        prefix = _resolve_leaf_prefix(base, by_name, cache, visiting)
        if prefix is not None:
            result = prefix
            break
    visiting.discard(name)
    cache[name] = result
    return result


def _emit_p003(
    rec: _ClassRecord,
    leaf_prefix: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    """Build a P003 finding for *rec* (missing or wrong-prefix code)."""
    if rec.code_value is None:
        node: ast.AST = rec.node
        message = (
            f"Class '{rec.name}' is a (transitive) subclass of '{leaf_prefix}'-category "
            f"AppError but does not declare its own 'code: ClassVar[str]'.  Without an "
            f"override, every raise of this class collapses to the bare leaf code "
            f"'{leaf_prefix}', making the failure impossible to triage from dashboards.  "
            f"Add a code that starts with '{leaf_prefix}_' (typed-error-prescription §4)."
        )
    else:
        node = rec.code_node or rec.node
        message = (
            f"Error code '{rec.code_value}' on class '{rec.name}' must start with the "
            f"parent leaf's category prefix '{leaf_prefix}_' (subclass of "
            f"'{leaf_prefix}'-category).  The category prefix lets dashboards and "
            f"on-call routing group by failure category without joining on the "
            f"category column (typed-error-prescription §4)."
        )
    return make_finding(
        filename=rec.file,
        rule_id="P003",
        node=node,
        message=message,
        directives=directives,
    )


# ── Public scan API ───────────────────────────────────────────────────────────


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
    checker = _PrescriptionChecker(filename=file, directives=directives)
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
    ``_LEAF_PREFIX_MAP``.  Cycle-safe via a per-resolution ``visiting`` set;
    results memoised.

    Pass 3 — emit P003 for every class that derives from a leaf and either
    omits its own ``code`` declaration or declares one that does not start
    with the leaf's category prefix + ``_``.  The 14 leaves themselves are
    exempt (their bare codes are the prefix definitions).
    """
    findings: list[Finding] = []

    # Pass 1
    file_directives: dict[Path, dict[int, _IgnoreDirective]] = {}
    file_records: dict[Path, list[_ClassRecord]] = {}
    by_name: dict[str, _ClassRecord] = {}

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
        checker = _PrescriptionChecker(filename=rel_str, directives=directives)
        checker.visit(tree)
        findings.extend(checker._findings)

        # Class registry for P003 — de-alias base names so aliased imports
        # of leaf classes (``from … import InternalError as _InternalError``)
        # don't hide the real ancestry.
        aliases = _collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        records = _collect_classes(tree, rel_str, aliases)
        file_records[path] = records
        # First-wins on name collisions across files — collisions in the SDK
        # are already resolved by careful module layout; a global same-name
        # collision would be a code-design problem outside this rule's scope.
        for rec in records:
            by_name.setdefault(rec.name, rec)

    # Pass 2 + 3 — resolve and emit P003
    cache: dict[str, str | None] = {}
    for path, records in file_records.items():
        directives = file_directives[path]
        for rec in records:
            if rec.name in _LEAF_PREFIX_MAP:
                # The leaf class itself — its bare code IS the prefix definition.
                continue
            leaf_prefix: str | None = None
            for base in rec.bases:
                leaf_prefix = _resolve_leaf_prefix(base, by_name, cache, set())
                if leaf_prefix is not None:
                    break
            if leaf_prefix is None:
                continue  # not derived from a typed AppError leaf — out of scope
            if rec.code_value is None:
                findings.append(_emit_p003(rec, leaf_prefix, directives))
            elif not rec.code_value.startswith(f"{leaf_prefix}_"):
                findings.append(_emit_p003(rec, leaf_prefix, directives))
    return findings


# ── CLI ───────────────────────────────────────────────────────────────────────


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
    parser.add_argument("--tool-version", default="0.3.0", metavar="VERSION")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    # Resolve every input to a flat path list so scan_all can build a single
    # cross-file class registry covering all of them — P003 requires this.
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


if __name__ == "__main__":
    sys.exit(main())

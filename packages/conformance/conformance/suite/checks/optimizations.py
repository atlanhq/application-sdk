"""O-series optimisation / recommendation checks — AST-based.

Scans Python source for below-the-prescription-bar recommendations (O001–O099).
Every check is purely deterministic: the same source text always produces the
same set of findings.

Currently implemented:

* ``O001`` StdlibJsonOverOrjson — ``json.dumps`` / ``json.loads`` call sites
  where ``json`` resolves to the stdlib module; prefer ``orjson``.

Inline suppression
------------------
Add a ``# conformance: ignore[O001] <reason>`` comment on the offending line or
the comment-only line immediately above it.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.checks.error_handling import discover
from conformance.suite.checks.error_handling._directives import (
    _IgnoreDirective,
    _parse_directives,
)
from conformance.suite.schema.findings import Finding, findings_to_report

SERIES = "O"

# json.<attr> call sites worth flagging.  orjson provides drop-in equivalents
# only for these two; json.dump/json.load (file-object APIs) have no orjson
# counterpart and are intentionally out of scope.
_JSON_CALL_ATTRS: frozenset[str] = frozenset({"dumps", "loads"})


def _collect_json_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(module_names, func_names)`` for stdlib ``json`` bindings.

    * ``module_names`` — local names bound to the stdlib ``json`` module via
      ``import json`` / ``import json as x`` (so ``x.dumps(...)`` is flaggable).
    * ``func_names`` — local names bound to ``json.dumps`` / ``json.loads`` via
      ``from json import dumps`` / ``from json import loads as y``.

    Walks the whole module (not just top-level) so function-local imports are
    covered.  Reassignment of a bound name is not tracked — consistent with the
    E-series import resolution and acceptably low false-positive in practice.
    """
    module_names: set[str] = set()
    func_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    module_names.add(alias.asname or "json")
        elif isinstance(node, ast.ImportFrom):
            if node.module != "json":
                continue
            for alias in node.names:
                if alias.name in _JSON_CALL_ATTRS:
                    func_names.add(alias.asname or alias.name)
    return frozenset(module_names), frozenset(func_names)


class _OptimizationChecker(ast.NodeVisitor):
    """Walk a module AST and emit O-series findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        json_module_names: frozenset[str],
        json_func_names: frozenset[str],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._json_module_names = json_module_names
        self._json_func_names = json_func_names
        self._findings: list[Finding] = []

    # ── O001 ──────────────────────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        attr: str | None = None
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _JSON_CALL_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in self._json_module_names
        ):
            attr = func.attr
        elif isinstance(func, ast.Name) and func.id in self._json_func_names:
            attr = func.id
        if attr is not None:
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="O001",
                    node=node,
                    message=(
                        f"json.{attr}() — prefer orjson.{attr}() (a core SDK "
                        "dependency, ~10x faster). Note orjson is not a drop-in: "
                        "dumps() returns bytes and uses option=orjson.OPT_* instead "
                        "of indent=/sort_keys=."
                    ),
                    directives=self._directives,
                )
            )
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public check API
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return all O-series findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    module_names, func_names = _collect_json_bindings(tree)
    directives = _parse_directives(text)
    checker = _OptimizationChecker(
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for O-series optimisation checks."""
    parser = argparse.ArgumentParser(
        description="O-series: scan Python files for optimisation/recommendation hits."
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

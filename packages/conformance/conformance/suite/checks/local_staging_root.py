"""P063 LocalStagingOutsideTemporaryPath — a run root built from the output-path
template and never rooted under ``TEMPORARY_PATH``.

``WORKFLOW_OUTPUT_PATH_TEMPLATE`` expands to an object-store **key**
(``artifacts/apps/<app>/workflows/<workflow>/<run>``).  Used as a local path it
is relative to the container working directory, which is commonly read-only on a
customer-hosted runtime.  The SDK's writable scratch prefix is
``TEMPORARY_PATH``, so a connector roots every local path built from that key
there: ``os.path.join(TEMPORARY_PATH, WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...))``.

Matching
--------
A call ``WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...)`` is flagged — through a
``constants.`` attribute spelling or an ``import ... as`` alias too — unless it
is rooted or is used as a key.

*Rooted* means one of:

* an enclosing expression also names ``TEMPORARY_PATH`` (the ``os.path.join``
  call, a ``/`` join, an f-string);
* the result is bound to a local name, and some expression in the same function
  names both that local and ``TEMPORARY_PATH``;
* the result is returned from a function whose every call in the module sits in
  an expression that names ``TEMPORARY_PATH``.

*Used as a key* means an enclosing call is ``get_object_store_prefix(...)`` or
``normalize_key(...)``, or the value is a ``storage_path=`` keyword: those take
object-store keys, never a local path.

Only the template is matched.  A literal ``artifacts/...`` string is not: apps
legitimately hold object-store keys with that spelling (``persistent-artifacts/``,
``argo-artifacts/``) that never touch the local disk.

The finding sits on the ``.format(...)`` call, so
``# conformance: ignore[P063] <reason>`` goes next to the key it justifies.
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
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_ID = "P063"

__all__ = ["RULE_ID", "SERIES", "discover", "main", "scan_path", "scan_text"]

_TEMPLATE = "WORKFLOW_OUTPUT_PATH_TEMPLATE"
_ROOT = "TEMPORARY_PATH"

#: Calls whose argument is an object-store key, not a local path.
_KEY_CALLS: frozenset[str] = frozenset({"get_object_store_prefix", "normalize_key"})

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

_MESSAGE = (
    "Local staging path built from WORKFLOW_OUTPUT_PATH_TEMPLATE without a "
    "TEMPORARY_PATH root. The template expands to an object-store key "
    "('artifacts/apps/<app>/workflows/...'); used as a local directory it is "
    "relative to the container working directory, which is commonly read-only "
    "on a customer-hosted runtime, so every fetch activity that writes under it "
    "fails with LocalVolumeUnwritableError. Root it: "
    "os.path.join(TEMPORARY_PATH, WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...)). "
    "Object-store calls still receive the key, because get_object_store_prefix "
    "and normalize_key strip TEMPORARY_PATH back off. If this value is only ever "
    "an object-store key, suppress with '# conformance: ignore[P063] <reason>'."
)


def _names(tree: ast.AST) -> set[str]:
    """Every bare name and attribute name used under *tree*."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _template_aliases(tree: ast.Module) -> frozenset[str]:
    """Names the template is imported under (``import X as Y`` aliases)."""
    names = {_TEMPLATE}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == _TEMPLATE and a.asname:
                    names.add(a.asname)
    return frozenset(names)


class _Checker:
    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        tree: ast.Module,
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._tree = tree
        self._aliases = _template_aliases(tree)
        self._parent: dict[ast.AST, ast.AST] = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                self._parent[c] = p
        self.findings: list[Finding] = []

    def _is_template_format(self, n: ast.AST) -> bool:
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            return False
        if n.func.attr != "format":
            return False
        v = n.func.value
        return (isinstance(v, ast.Name) and v.id in self._aliases) or (
            isinstance(v, ast.Attribute) and v.attr == _TEMPLATE
        )

    def _enclosing_func(self, n: ast.AST) -> ast.AST:
        cur = self._parent.get(n)
        while cur is not None and not isinstance(cur, _FUNCS):
            cur = self._parent.get(cur)
        return cur if cur is not None else self._tree

    def _expr_chain(self, n: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
        """Ancestors of *n* up to its statement, and that statement."""
        chain: list[ast.AST] = []
        cur = self._parent.get(n)
        while cur is not None and not isinstance(cur, ast.stmt):
            chain.append(cur)
            cur = self._parent.get(cur)
        return chain, cur

    def _used_as_key(self, n: ast.AST, chain: list[ast.AST]) -> bool:
        for a in chain:
            if isinstance(a, ast.Call) and _call_name(a) in _KEY_CALLS:
                return True
            if isinstance(a, ast.keyword) and a.arg == "storage_path":
                return True
        return False

    def _local_is_rooted(self, local: str, scope: ast.AST) -> bool:
        for e in ast.walk(scope):
            if isinstance(e, ast.expr):
                names = _names(e)
                if local in names and _ROOT in names:
                    return True
        return False

    def _returned_value_is_rooted(self, func: ast.AST) -> bool:
        if not isinstance(func, _FUNCS):
            return False
        calls = [
            c
            for c in ast.walk(self._tree)
            if isinstance(c, ast.Call) and _call_name(c) == func.name
        ]
        if not calls:
            return False
        for c in calls:
            chain, _ = self._expr_chain(c)
            if not any(_ROOT in _names(a) for a in chain):
                return False
        return True

    def _is_rooted(self, n: ast.Call) -> bool:
        chain, stmt = self._expr_chain(n)
        if any(_ROOT in _names(a) for a in chain):
            return True
        if self._used_as_key(n, chain):
            return True
        scope = self._enclosing_func(n)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            local_names = [t.id for t in targets if isinstance(t, ast.Name)]
            return any(self._local_is_rooted(name, scope) for name in local_names)
        if isinstance(stmt, ast.Return):
            return self._returned_value_is_rooted(scope)
        return False

    def check(self) -> None:
        for n in ast.walk(self._tree):
            if self._is_template_format(n) and not self._is_rooted(n):
                self.findings.append(
                    make_finding(
                        filename=self._filename,
                        rule_id=RULE_ID,
                        node=n,
                        message=_MESSAGE,
                        directives=self._directives,
                    )
                )
        self.findings.sort(key=lambda f: (f.line, f.column))


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P063 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    checker = _Checker(file, _parse_directives(text), tree)
    checker.check()
    return checker.findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P063 findings."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, rel.as_posix())


main = make_cli_main(
    scan_text,
    description="P063: flag local staging paths built from the output-path template "
    "without a TEMPORARY_PATH root.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())

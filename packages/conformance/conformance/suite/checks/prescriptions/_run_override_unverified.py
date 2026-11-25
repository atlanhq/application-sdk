"""P068 SqlAppRunOverrideUnverified — a ``SqlApp.run()`` replacement that skips verification.

``SqlApp.run()`` ends in ``finalize_extraction``, which checks every
``transformed_file`` the transforms declared is present under the
``transformed_data_prefix`` it returns.  Without that check a tree short by one
entity looks exactly like a source that has none, and publish archives the
missing assets as removed-from-source.

Overriding ``run()`` is a documented path, and an override that calls
``super().run()`` keeps the check.  One that replaces it outright has to call
``finalize_extraction`` itself.  This rule flags a ``run`` defined on a
``SqlApp`` subclass whose body awaits neither.

It does **not** flag an override for skipping ``super().run()`` alone: most of
the fleet's replacements (Db2, Teradata, Trino) drive their own sequencing and
end in ``finalize_extraction``, which is the documented shape.

Resolution
----------
``SqlApp`` counts only when the defining module imports it from
``application_sdk`` (``ClassRecord.sdk_app_bases``); subclasses are followed
across files, transitively, so an override on an app's own intermediate base
is checked too.  A base name defined in more than one scanned file resolves
to the class in the module it is imported from; when that cannot be decided,
the base is not followed (a missed finding, never a wrong one).

Only an **awaited** call counts — ``finalize_extraction`` and ``run`` are
coroutines, and a call that is not awaited verifies nothing — and only one
made by ``run`` itself, not inside a nested function or class it defines.
The receiver must be the app: ``await super().run(...)``,
``await <Ancestor>.run(self, ...)``, or ``await self.finalize_extraction(...)``
/ ``super().finalize_extraction(...)`` / ``<Ancestor>.finalize_extraction(self,
...)``.  Where in ``run`` the call sits is not checked: the rule is about
whether the override verifies at all, not about which path reaches it.

SDK gate
--------
Silent unless the app's ``uv.lock`` resolves an ``atlan-application-sdk`` that
ships ``finalize_extraction`` (:data:`FINALIZE_SDK_FLOOR`): the fix prescribes
it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._version import locked_sdk_reaches
from conformance.suite.schema.findings import Finding

from ._error_code_prefix import ClassRecord, _provenance_record

_RULE_ID = "P068"
_SQL_APP = "SqlApp"

#: First application-sdk release with ``SqlApp.finalize_extraction``.
FINALIZE_SDK_FLOOR = (3, 33, 2)


def _parent(
    base: str,
    owner: ClassRecord,
    by_name_all: Mapping[str, Sequence[ClassRecord]],
) -> ClassRecord | None:
    """The scanned class *owner*'s base *base* names, or ``None``.

    A name defined once is that class.  A name defined in several files is
    the one in the module *owner* imports it from (or *owner*'s own file);
    if that cannot be decided the base is not followed.
    """
    records = by_name_all.get(base, ())
    if len(records) == 1:
        return records[0]
    return _provenance_record(base, owner, by_name_all)


def _sql_app_ancestors(
    rec: ClassRecord,
    by_name_all: Mapping[str, Sequence[ClassRecord]],
    visiting: frozenset[int] = frozenset(),
) -> frozenset[str] | None:
    """Names on *rec*'s base chain up to the SDK's ``SqlApp``, or ``None``.

    ``None`` when no chain reaches an ``SqlApp`` imported from the SDK.
    """
    if id(rec) in visiting:
        return None
    visiting = visiting | {id(rec)}
    for base in rec.bases:
        if base == _SQL_APP and base in rec.sdk_app_bases:
            return frozenset({_SQL_APP})
        parent = _parent(base, rec, by_name_all)
        if parent is None or parent is rec:
            continue
        found = _sql_app_ancestors(parent, by_name_all, visiting)
        if found is not None:
            return found | {base}
    return None


def _run_method(node: ast.ClassDef) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for stmt in node.body:
        if (
            isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            and stmt.name == "run"
        ):
            return stmt
    return None


def _is_super_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
    )


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Every node in *func*'s body, not descending into nested scopes."""
    stack: list[ast.AST] = [s for s in func.body if not isinstance(s, _NESTED_SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, _NESTED_SCOPES)
        )


def _is_app_receiver(
    receiver: ast.expr, call: ast.Call, ancestors: frozenset[str]
) -> bool:
    """``super()``, ``self``, or ``<Ancestor>`` called with ``self`` first."""
    if _is_super_call(receiver):
        return True
    if not isinstance(receiver, ast.Name):
        return False
    if receiver.id in ancestors:
        first = call.args[0] if call.args else None
        return isinstance(first, ast.Name) and first.id == "self"
    return False


def _verifies(
    func: ast.FunctionDef | ast.AsyncFunctionDef, ancestors: frozenset[str]
) -> bool:
    """True if *func* awaits the base run or ``finalize_extraction`` itself."""
    for node in _own_nodes(func):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        attr, receiver = call.func.attr, call.func.value
        if attr == "finalize_extraction":
            if isinstance(receiver, ast.Name) and receiver.id == "self":
                return True
            if _is_app_receiver(receiver, call, ancestors):
                return True
        elif attr == "run" and _is_app_receiver(receiver, call, ancestors):
            return True
    return False


def check_p068(
    file_records: dict[Path, list[ClassRecord]],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P068 for each ``SqlApp`` subclass whose ``run`` never verifies."""
    if not locked_sdk_reaches(root, FINALIZE_SDK_FLOOR):
        return []
    by_name_all: dict[str, list[ClassRecord]] = {}
    for records in file_records.values():
        for rec in records:
            by_name_all.setdefault(rec.name, []).append(rec)
    findings: list[Finding] = []
    for path, records in file_records.items():
        directives = file_directives.get(path, {})
        for rec in records:
            run = _run_method(rec.node)
            if run is None:
                continue
            ancestors = _sql_app_ancestors(rec, by_name_all)
            if ancestors is None or _verifies(run, ancestors):
                continue
            findings.append(
                make_finding(
                    filename=rec.file,
                    rule_id=_RULE_ID,
                    node=run,
                    message=(
                        f"'{rec.name}.run()' replaces SqlApp.run() without calling "
                        "super().run() or self.finalize_extraction(...), so nothing "
                        "checks the transformed tree is whole before publish diffs "
                        "the tenant against it. End the override with "
                        "`return await self.finalize_extraction(base, extra)`."
                    ),
                    directives=directives,
                )
            )
    return findings

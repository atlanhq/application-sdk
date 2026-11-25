"""P068 SqlAppRunOverrideUnverified — a ``SqlApp.run()`` replacement that skips verification.

``SqlApp.run()`` ends in ``finalize_extraction``, which checks every
``transformed_file`` the transforms declared is present under the
``transformed_data_prefix`` it returns.  Without that check a tree short by one
entity looks exactly like a source that has none, and publish archives the
missing assets as removed-from-source.

Overriding ``run()`` is a documented path, and an override that calls
``super().run()`` keeps the check.  One that replaces it outright has to call
``finalize_extraction`` itself.  This rule flags a ``run`` defined on a
``SqlApp`` subclass whose body calls neither.

It does **not** flag an override for skipping ``super().run()`` alone: most of
the fleet's replacements (Db2, Teradata, Trino) drive their own sequencing and
end in ``finalize_extraction``, which is the documented shape.

Resolution
----------
``SqlApp`` counts only when the defining module imports it from
``application_sdk`` (``ClassRecord.sdk_app_bases``); subclasses are followed
across files by bare name through ``scan_all``'s registry, transitively, so an
override on an app's own intermediate base is checked too.  A call anywhere in
the ``run`` body counts — ``super().run(...)``, ``<Ancestor>.run(self, ...)``,
or ``<anything>.finalize_extraction(...)``.  The rule is about whether the
override verifies at all, not about which path reaches it.

SDK gate
--------
Silent unless the app's ``uv.lock`` resolves an ``atlan-application-sdk`` that
ships ``finalize_extraction`` (:data:`FINALIZE_SDK_FLOOR`): the fix prescribes
it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._version import locked_sdk_reaches
from conformance.suite.schema.findings import Finding

from ._error_code_prefix import ClassRecord

_RULE_ID = "P068"
_SQL_APP = "SqlApp"

#: First application-sdk release with ``SqlApp.finalize_extraction``.
FINALIZE_SDK_FLOOR = (3, 33, 2)


def _sql_app_ancestors(
    rec: ClassRecord,
    by_name: dict[str, ClassRecord],
    visiting: frozenset[str] = frozenset(),
) -> frozenset[str] | None:
    """Names on *rec*'s base chain up to the SDK's ``SqlApp``, or ``None``.

    ``None`` when no chain reaches an ``SqlApp`` imported from the SDK.
    """
    if rec.name in visiting:
        return None
    visiting = visiting | {rec.name}
    for base in rec.bases:
        if base == _SQL_APP and base in rec.sdk_app_bases:
            return frozenset({_SQL_APP})
        parent = by_name.get(base)
        if parent is None or parent is rec:
            continue
        found = _sql_app_ancestors(parent, by_name, visiting)
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


def _verifies(func: ast.AST, ancestors: frozenset[str]) -> bool:
    """True if *func*'s body reaches the base run or ``finalize_extraction``."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr, receiver = node.func.attr, node.func.value
        if attr == "finalize_extraction":
            return True
        if attr != "run":
            continue
        if _is_super_call(receiver):
            return True
        if isinstance(receiver, ast.Name) and receiver.id in ancestors:
            return True
    return False


def check_p068(
    file_records: dict[Path, list[ClassRecord]],
    by_name: dict[str, ClassRecord],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P068 for each ``SqlApp`` subclass whose ``run`` never verifies."""
    if not locked_sdk_reaches(root, FINALIZE_SDK_FLOOR):
        return []
    findings: list[Finding] = []
    for path, records in file_records.items():
        directives = file_directives.get(path, {})
        for rec in records:
            run = _run_method(rec.node)
            if run is None:
                continue
            ancestors = _sql_app_ancestors(rec, by_name)
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

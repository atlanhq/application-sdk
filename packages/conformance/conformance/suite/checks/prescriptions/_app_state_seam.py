"""P027 AppStateAsCrossTaskChannel — ``get_app_state`` read with no writer.

``app_state`` is an in-memory, per-execution-id bag (``App.get_app_state`` /
``set_app_state``).  Apps reach for it as a data conduit between tasks, but it
silently no-ops across activity/worker boundaries.  The high-signal failure
shape is a ``get_app_state(KEY)`` whose ``KEY`` is never written by any
``set_app_state(KEY, <non-None value>)`` anywhere in the app — the read always
falls through to its default.  A key whose *only* writer stores ``None`` (an
"claim ownership" placeholder whose real populating write never lands) counts as
having no writer.  Cross-task payload data belongs in the typed entrypoint/task
contract, not this side channel.

Cross-file (``scan_all`` pass): keys are frequently module-level string
constants defined in one file and read in another, so resolution uses an
app-wide ``NAME -> "literal"`` map built from module-level assignments.  Keys
that do not resolve to a string literal (dynamic) are conservatively ignored on
both the read and write side.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_GET = "get_app_state"
_SET = "set_app_state"


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _module_str_constants(tree: ast.AST) -> dict[str, str]:
    """Map module-level ``NAME = "literal"`` assignments to their string value."""
    consts: dict[str, str] = {}
    if not isinstance(tree, ast.Module):
        return consts
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            consts.setdefault(target.id, value.value)
    return consts


def check_p027(
    file_trees: dict[Path, ast.AST],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P027 for ``get_app_state(KEY)`` reads with no matching writer."""
    # App-wide constant map (key constants are often defined far from the read).
    consts: dict[str, str] = {}
    for tree in file_trees.values():
        for name, val in _module_str_constants(tree).items():
            consts.setdefault(name, val)

    def resolve(arg: ast.expr) -> str | None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return consts.get(arg.id)
        return None

    # Pass 1 — collect every *populating* writer key across the app. A writer
    # that only stores None (a placeholder "claim ownership" write) does not
    # populate the channel, so it does not count.
    written: set[str] = set()
    reads: list[tuple[Path, ast.Call, str]] = []
    for path, tree in file_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = _call_name(node.func)
            if name == _SET:
                key = resolve(node.args[0])
                value = node.args[1] if len(node.args) >= 2 else None
                writes_none = value is None or (
                    isinstance(value, ast.Constant) and value.value is None
                )
                if key is not None and not writes_none:
                    written.add(key)
            elif name == _GET:
                key = resolve(node.args[0])
                if key is not None:
                    reads.append((path, node, key))

    # Pass 2 — a read whose key is never written is a dead side channel.
    findings: list[Finding] = []
    for path, node, key in reads:
        if key in written:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        findings.append(
            make_finding(
                filename=str(rel),
                rule_id="P027",
                node=node,
                message=(
                    f"get_app_state({key!r}) has no matching set_app_state({key!r}) "
                    f"writer anywhere in the app. app_state is in-memory and scoped "
                    f"to one execution, so this read always falls through to its "
                    f"default. Pass cross-task data through the typed contract instead."
                ),
                directives=file_directives.get(path, {}),
            )
        )
    return findings

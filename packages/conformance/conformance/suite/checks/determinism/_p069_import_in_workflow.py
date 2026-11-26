"""P069 — ImportInWorkflow.

Flags an ``import`` / ``from … import`` statement inside a workflow-context method
of an ``App`` subclass.

Temporal builds a workflow instance on the worker's event-loop thread, where no
deadlock timer runs, so the imports at the top of a module cost nothing at run
time.  Every later activation runs on a workflow thread that Temporal interrupts
after 2 seconds (``TMPRL1101``).  An import inside the method body runs there, and
the sandbox re-executes any module it has not loaded yet — the app's own package
is never passed through.  On a cold, busy worker that import overruns the timer,
and the interrupt is raised into the thread while importlib may hold its global
lock.  The lock is never released, every later import in the process blocks, and
the whole worker stops making progress while its pod stays up.

Imports guarded by ``if TYPE_CHECKING:`` never run and are not flagged.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._workflow_methods import workflow_method_nodes

RULE_ID = "P069"

_HINT = (
    "Move it to module level: inside workflow code it runs on the workflow thread "
    "under Temporal's 2s deadlock timer, and an interrupt mid-import can leave "
    "importlib's lock held and freeze the whole worker."
)


def _is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Every import under *node*, skipping ``if TYPE_CHECKING:`` blocks."""
    found: list[ast.Import | ast.ImportFrom] = []
    for child in ast.iter_child_nodes(node):
        if _is_type_checking_guard(child):
            continue
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            found.append(child)
        found.extend(_imports(child))
    return found


def _module_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return "." * node.level + (node.module or "")
    return ", ".join(alias.name for alias in node.names)


def check_p069(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P069 findings for imports inside workflow-context methods."""
    return [
        make_finding(
            filename=filename,
            rule_id=RULE_ID,
            node=node,
            message=f"Import of '{_module_name(node)}' in workflow context. {_HINT}",
            directives=directives,
        )
        for method in workflow_method_nodes(tree)
        for node in _imports(method)
    ]

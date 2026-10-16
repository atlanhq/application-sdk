"""P062 TempScratchNotCleanedUp — ``mkdtemp``/``mkstemp`` scratch not removed on every exit.

``tempfile.mkdtemp`` and ``tempfile.mkstemp`` hand back a path the caller owns:
nothing removes it unless the caller does, and a removal at the end of the happy
path is skipped by every exception before it.  A retried or failed activity then
leaves its scratch behind on the worker's disk, one copy per attempt.

A call is flagged when its result is bound to a local name
(``d = tempfile.mkdtemp(...)``, ``d = Path(tempfile.mkdtemp(...))``,
``fd, path = tempfile.mkstemp(...)``) and that scratch is neither:

* removed inside a ``finally`` — ``shutil.rmtree`` / ``os.remove`` /
  ``os.unlink`` / ``Path.unlink``, a removal-named helper, or a removal handed to
  ``asyncio.to_thread`` — of the scratch, a path built from it, or an alias of
  it; nor
* removed both in an ``except`` that catches everything (bare, ``Exception``,
  ``BaseException``) and on the success path, which covers every exit too; nor
* registered for cleanup (``stack.callback(shutil.rmtree, d)``,
  ``atexit.register(...)``, ``weakref.finalize(...)``), which runs on every exit
  path; nor
* passed out of the function (returned, yielded, stored on an attribute or
  subscript, ``global``/``nonlocal``, or built into something that is): its
  owner is then the receiver — a helper returning a fresh scratch dir, or a task
  returning ``FileReference``\\ s for the files inside it.

The context-manager forms (``TemporaryDirectory``, ``NamedTemporaryFile``)
clean up after themselves and are not graded here; where they land is P061's
concern.

Known limits (biased toward zero false positives at BLOCK):

* A result not bound to a name (``work(tempfile.mkdtemp())``) is not graded.
* A removal only in narrower handlers (``except OSError``) plus the success path
  still reads as success-path-only: other exceptions skip both.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._local_files import (
    ModuleContext,
    ScopeFlow,
    TempfileBindings,
    iter_scopes,
    runs_on_worker,
)
from conformance.suite.schema.findings import Finding

#: The graded functions, and which element of the result is the path.
_SCRATCH: dict[str, int | None] = {"mkdtemp": None, "mkstemp": 1}


def _message(name: str, token: str, removed_at: int | None) -> str:
    if name == "mkstemp":
        removal = f"os.remove({token})"
        alternative = "tempfile.NamedTemporaryFile(dir=...)"
    else:
        removal = f"shutil.rmtree({token}, ignore_errors=True)"
        alternative = "tempfile.TemporaryDirectory(dir=...)"
    if removed_at is not None:
        cause = (
            f"is removed at line {removed_at} only when nothing raises before it, "
            "so a failed or retried attempt leaves it on the worker's disk"
        )
    else:
        cause = (
            "is never removed in this function, so it stays on the worker's disk "
            "after the function returns"
        )
    return (
        f"tempfile.{name}() scratch {token} {cause}. Remove it in a finally "
        f"(try: ... finally: {removal}), or use {alternative}, which removes it on "
        "every exit."
    )


def check_p062(
    tree: ast.Module,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P062 for ``mkdtemp``/``mkstemp`` scratch not removed on every exit path."""
    if not runs_on_worker(filename):
        return []
    bindings = TempfileBindings(tree)
    if bindings.empty:
        return []
    context = ModuleContext.of(tree)
    findings: list[Finding] = []
    for scope in iter_scopes(tree):
        flow: ScopeFlow | None = None
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            name = bindings.resolve(node.func)
            if name not in _SCRATCH:
                continue
            if flow is None:
                flow = context.flow(scope)
            if not any(site.node is node for site in flow.calls):
                continue  # belongs to a nested scope, graded there
            token = flow.bound_name(node, element=_SCRATCH[name])
            if token is None:
                continue
            tokens = frozenset({token})
            if flow.escapes(tokens) or flow.removed_on_every_exit(tokens, after=node):
                continue
            removal = flow.removal_of(tokens, after=node)
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P062",
                    node=node,
                    message=_message(name, token, removal.line if removal else None),
                    directives=directives,
                )
            )
    return sorted(findings, key=lambda f: (f.line, f.column))

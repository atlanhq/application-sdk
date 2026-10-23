"""P060 UploadedFileNotDeleted — a local file or directory kept on disk after upload.

``application_sdk.storage.ops.upload_file`` keeps the local file after a
successful upload unless the caller passes ``retain_local_copy=False`` (the
keyword defaults to ``True``), and even then it deletes only a file under
``TEMPORARY_PATH``; ``upload_prefix`` does the same for every file it uploads.
An app that uploads as it goes therefore keeps every file it has already
uploaded on the worker's disk for the rest of the run.

A call is flagged when all of these hold:

* it resolves, through the module's import bindings, to the SDK's
  ``upload_file`` or ``upload_prefix`` (see
  :data:`~conformance.suite.checks._local_files.SDK_STORAGE_MODULES`), so an
  app's own ``upload_file`` or a client method of that name never matches;
* ``retain_local_copy`` is omitted or the literal ``True`` — ``False`` is
  compliant, and any other expression or a ``**kwargs`` splat is left alone
  because its value is not known statically;
* nothing after the upload in the same function removes the uploaded path, a
  path it was built from, or an alias of it — ``os.remove`` / ``os.unlink`` /
  ``shutil.rmtree`` / ``Path.unlink``, a removal-named helper, or a removal
  handed to ``asyncio.to_thread`` or a cleanup callback, ``finally`` included;
* the path does not leave the function (returned, yielded, stored on an
  attribute or subscript, ``global``/``nonlocal``): whoever receives it — say,
  the next task, through a returned ``FileReference`` — owns it.

The message names the uploaded path and, when the function reads it again after
the upload, the line of that last read: the removal belongs after it.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._local_files import (
    LOCAL_ARG,
    ModuleContext,
    ScopeFlow,
    StorageBindings,
    call_arg,
    has_kwargs_splat,
    iter_scopes,
    runs_on_worker,
)
from conformance.suite.schema.findings import Finding

_UPLOADS = ("upload_file", "upload_prefix")
_KWARG = "retain_local_copy"


def _short(expr: ast.expr) -> str:
    text = ast.unparse(expr)
    return text if len(text) <= 60 else text[:57] + "..."


def _literal_removed(flow: ScopeFlow, local: ast.expr, call: ast.Call) -> bool:
    """A path with no name in it (a literal, a call result) is removed only by a
    removal that spells the same expression."""
    wanted = ast.dump(local)
    end = (call.lineno, call.col_offset)
    return any(
        isinstance(removal.node, ast.Call)
        and removal.position > end
        and any(ast.dump(node) == wanted for node in ast.walk(removal.node))
        for removal in flow.removals
    )


def _kept(
    call: ast.Call, name: str, flow: ScopeFlow
) -> tuple[ast.expr, int | None] | None:
    """``(local path, last read line)`` when *call* leaves its source on disk."""
    if has_kwargs_splat(call):
        return None
    retain = next((kw.value for kw in call.keywords if kw.arg == _KWARG), None)
    if retain is not None and not (
        isinstance(retain, ast.Constant) and retain.value is True
    ):
        return None  # literal False, or a value not known statically
    local = call_arg(call, *LOCAL_ARG[name])
    if local is None:
        return None
    tokens = flow.tokens(local)
    if not tokens:
        return None if _literal_removed(flow, local, call) else (local, None)
    if flow.escapes(tokens) or flow.removal_of(tokens, after=call) is not None:
        return None
    return local, flow.last_read(tokens, call)


def _message(
    call: ast.Call,
    name: str,
    local: ast.expr,
    last_read: int | None,
    parameter: str | None,
) -> str:
    written = ast.unparse(call.func)
    retain = next((kw.value for kw in call.keywords if kw.arg == _KWARG), None)
    why = (
        "it passes retain_local_copy=True"
        if retain is not None
        else "retain_local_copy defaults to True"
    )
    if name == "upload_prefix":
        fix = (
            "Remove the directory once the upload returns "
            f"(shutil.rmtree({_short(local)}, ignore_errors=True)), or pass "
            "retain_local_copy=False when it is under TEMPORARY_PATH"
        )
    else:
        fix = (
            f"Delete the file once the upload returns (os.remove({_short(local)}) "
            "or Path.unlink(missing_ok=True)), or pass retain_local_copy=False "
            "when it is under TEMPORARY_PATH"
        )
    later = (
        f" It is read again at line {last_read}, so remove it after that read."
        if last_read is not None
        else ""
    )
    owner = (
        f" The path comes from the caller (parameter {parameter}): remove it here "
        "if no caller reads it after this call, otherwise in the callers."
        if parameter is not None
        else ""
    )
    return (
        f"{written}() leaves {_short(local)} on the worker's disk: {why} and "
        "nothing after the upload in this function removes it, so every file a "
        f"run uploads stays on disk for the rest of the run. {fix} (the SDK "
        f"deletes nothing outside it).{later}{owner}"
    )


def check_p060(
    tree: ast.Module,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P060 for SDK uploads whose local source is never removed."""
    if not runs_on_worker(filename):
        return []
    bindings = StorageBindings(tree)
    if bindings.empty:
        return []
    context = ModuleContext.of(tree)
    findings: list[Finding] = []
    for scope in iter_scopes(tree):
        flow = context.flow(scope)
        for site in flow.calls:
            name = bindings.resolve(site.node)
            if name not in _UPLOADS:
                continue
            kept = _kept(site.node, name, flow)
            if kept is None:
                continue
            local, last_read = kept
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P060",
                    node=site.node,
                    message=_message(
                        site.node,
                        name,
                        local,
                        last_read,
                        flow.parameter_source(flow.tokens(local)),
                    ),
                    directives=directives,
                )
            )
    return sorted(findings, key=lambda f: (f.line, f.column))

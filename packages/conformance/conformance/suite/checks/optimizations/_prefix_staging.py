"""O010 WholePrefixStagedOnDisk — a whole prefix downloaded, then re-uploaded.

``download_prefix(prefix, local_dir)`` followed, in the same function, by
``upload_prefix(local_dir, ...)`` of that directory (or a directory under it, or
one holding it) puts every object under the prefix on the worker's disk at once
before any of it leaves.  The disk the step needs then grows with the largest
prefix any tenant has, rather than with a batch.

Moving data across stores through the worker is expected — a self-deployed
runtime hands data from the customer's deployment store to Atlan's upstream
store, or one cloud to another — and is not what this rule grades; staging the
whole prefix to do it is.  So:

* only the SDK's bulk helpers count (``download_prefix`` then
  ``upload_prefix``, resolved through the module's import bindings).  A
  per-file move (``download_file`` → ``upload_file`` → delete) and the SDK's own
  cross-store hand-off (``App.upload`` / ``App.upload_refs`` and the
  ``application_sdk.storage.transfer`` helpers behind them, which move one
  object at a time) are never matched;
* a loop that downloads, uploads *and removes* its local batch in the same
  iteration is bounded by the batch, so it is not flagged.

The finding sits on the ``upload_prefix`` call and names the ``download_prefix``
line.  When both calls provably use the same store (both omit ``store=``, or pass
the same expression), the message also asks whether the copy can stay inside the
store.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._local_files import (
    LOCAL_ARG,
    CallSite,
    ModuleContext,
    ScopeFlow,
    StorageBindings,
    call_arg,
    iter_scopes,
    runs_on_worker,
    same_store,
)
from conformance.suite.schema.findings import Finding


def _same_tree(flow: ScopeFlow, a: ast.expr | None, b: ast.expr | None) -> bool:
    if a is None or b is None:
        return False
    ta, tb = flow.tokens(a), flow.tokens(b)
    if ta and tb:
        return flow.related(ta, tb)
    return ast.dump(a) == ast.dump(b)


def _shared_loop(down: CallSite, up: CallSite) -> ast.AST | None:
    """The innermost loop enclosing both calls, if any."""
    shared = [loop for loop in down.loops if loop in up.loops]
    return shared[-1] if shared else None


def _message(down: ast.Call, up: ast.Call, local: ast.expr) -> str:
    text = ast.unparse(local)
    text = text if len(text) <= 60 else text[:57] + "..."
    store = (
        " Both calls use the same store, so also check whether the copy can stay "
        "inside the store."
        if same_store(down, up)
        else " Moving across stores through the worker is expected; staging the "
        "whole prefix to do it is the problem."
    )
    return (
        f"upload_prefix() re-uploads {text}, which download_prefix() at line "
        f"{down.lineno} filled with a whole prefix, so every object under it sits "
        "on the worker's disk at once. Move it in bounded batches instead: group "
        "the keys into batches capped by bytes (256 MiB slices are a proven "
        "size), and per batch download, upload (the bulk helpers still run "
        "max_concurrency=4) and delete the batch before the next. Or pipeline "
        "single files with about 4 in flight (download, upload, delete), which "
        f"overlaps download and upload and holds only a few files on disk.{store}"
    )


def check_o010(
    tree: ast.Module,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O010 where a downloaded prefix is staged whole and re-uploaded."""
    if not runs_on_worker(filename):
        return []
    bindings = StorageBindings(tree)
    if bindings.empty:
        return []
    context = ModuleContext.of(tree)
    findings: list[Finding] = []
    for scope in iter_scopes(tree):
        flow = context.flow(scope)
        downloads: list[CallSite] = []
        uploads: list[CallSite] = []
        for site in flow.calls:
            name = bindings.resolve(site.node)
            if name == "download_prefix":
                downloads.append(site)
            elif name == "upload_prefix":
                uploads.append(site)
        for up in uploads:
            up_local = call_arg(up.node, *LOCAL_ARG["upload_prefix"])
            for down in downloads:
                if (down.node.lineno, down.node.col_offset) >= (
                    up.node.lineno,
                    up.node.col_offset,
                ):
                    continue
                down_local = call_arg(down.node, *LOCAL_ARG["download_prefix"])
                if not _same_tree(flow, down_local, up_local):
                    continue
                loop = _shared_loop(down, up)
                if loop is not None:
                    batch = flow.tokens(down_local) | flow.tokens(up_local)
                    if flow.removal_of(batch, within=loop) is not None:
                        continue  # one bounded batch per iteration
                assert up_local is not None
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="O010",
                        node=up.node,
                        message=_message(down.node, up.node, up_local),
                        directives=directives,
                    )
                )
                break
    return sorted(findings, key=lambda f: (f.line, f.column))

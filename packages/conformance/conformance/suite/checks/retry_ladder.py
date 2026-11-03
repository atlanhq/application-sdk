"""P060 HandRolledRetryLadder — a loop with ``try``/``except`` and a sleep.

FND-4284 moved the SDK's hand-rolled retry ladders onto one helper,
``application_sdk.common.backoff`` (``Backoff`` + ``retry_async``), because the
ladders' jitter conventions had drifted apart and several had none.  This check
stops a new ladder from growing back beside the helper.

Matching
--------
A sleep call is flagged when an enclosing ``for`` / ``while`` / ``async for``
loop's **body** — at any depth, but not inside a nested ``def``, ``async def``,
``lambda`` or ``class``, which run on their own schedule — also holds a ``try``
statement with at least one ``except`` handler.  The two may sit anywhere in the
loop: the sleep in the ``except`` block, after the ``try``, or in a nested
``if``.  A loop's ``else:`` clause runs once, not per attempt, so it is not
searched.

A sleep call is ``asyncio.sleep(...)``, ``time.sleep(...)`` or
``anyio.sleep(...)`` — through an ``import asyncio as aio`` alias too — or a
bare ``sleep(...)`` (or its ``as`` alias) imported from one of those modules.
``workflow.sleep`` is a durable Temporal timer and never matches: its receiver
is not one of the three modules.

The finding is reported at the **sleep call's** line, once per sleep call even
when nested loops both qualify, so ``# conformance: ignore[P060] <reason>`` sits
next to the delay it justifies.

Not flagged
-----------
* ``application_sdk/common/backoff.py`` — the one loop allowed to walk a ladder.
* ``sleep(0)`` / ``sleep(0.0)`` — yield-to-the-event-loop, not a delay.
* A loop with a sleep but no ``try``/``except`` — a plain poller or flush loop.
* A ``try`` with only a ``finally`` — it retries nothing.

Inline suppression
------------------
``# conformance: ignore[P060] <reason>`` on the sleep's line, or on the
comment-only line directly above it.
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
RULE_ID = "P060"

__all__ = ["RULE_ID", "SERIES", "discover", "main", "scan_path", "scan_text"]

#: Modules whose ``sleep`` is an in-process delay.
_SLEEP_MODULES: frozenset[str] = frozenset({"asyncio", "time", "anyio"})

#: The helper module itself — the one place a ladder loop is allowed.
_HELPER_SUFFIX = "application_sdk/common/backoff.py"

_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

_MESSAGE = (
    "Hand-rolled retry ladder: this sleep sits in a loop that also catches "
    "exceptions. Use application_sdk.common.backoff.retry_async(call, "
    "backoff=Backoff(...), retry_if=...) — or keep the loop and take the delay "
    "from Backoff.delay(retry) when it has its own control flow (a supervisor "
    "that resets its streak, a loop racing a shutdown event). Hand-rolled "
    "ladders drift apart on jitter, and an un-jittered one makes every worker "
    "that lost the same dependency retry in lockstep — a fleet-wide retry "
    "storm. Suppress a loop that is not a retry ladder with "
    "'# conformance: ignore[P060] <reason>'."
)


def _collect_sleep_aliases(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return (module names, bare sleep names) that resolve to an in-process sleep.

    The plain module names are always included so ``asyncio.sleep`` matches even
    in a fragment that never imports ``asyncio`` (a test fixture, say).
    """
    modules: set[str] = set(_SLEEP_MODULES)
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SLEEP_MODULES and alias.asname:
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module in _SLEEP_MODULES:
            for alias in node.names:
                if alias.name == "sleep":
                    bare.add(alias.asname or "sleep")
    return frozenset(modules), frozenset(bare)


def _is_zero(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value == 0
    )


class _LadderChecker:
    """Find sleep calls inside loops that also catch exceptions."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        modules: frozenset[str],
        bare: frozenset[str],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._modules = modules
        self._bare = bare
        self._flagged: set[int] = set()
        self.findings: list[Finding] = []

    def _is_sleep(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            matched = (
                func.attr == "sleep"
                and isinstance(func.value, ast.Name)
                and func.value.id in self._modules
            )
        elif isinstance(func, ast.Name):
            matched = func.id in self._bare
        else:
            matched = False
        if not matched:
            return False
        if node.args and _is_zero(node.args[0]):
            return False
        return not any(
            kw.arg in {"delay", "seconds"} and _is_zero(kw.value)
            for kw in node.keywords
        )

    def _loop_scope(self, loop: ast.For | ast.AsyncFor | ast.While) -> list[ast.AST]:
        """Every node in *loop*'s body, stopping at nested function/class scopes."""
        out: list[ast.AST] = []
        stack: list[ast.AST] = list(loop.body)
        while stack:
            node = stack.pop()
            out.append(node)
            if isinstance(node, _SCOPES):
                continue
            stack.extend(ast.iter_child_nodes(node))
        return out

    def check(self, tree: ast.Module) -> None:
        for loop in ast.walk(tree):
            if not isinstance(loop, _LOOPS):
                continue
            nodes = self._loop_scope(loop)
            catches = any(
                isinstance(n, (ast.Try, ast.TryStar)) and n.handlers for n in nodes
            )
            if not catches:
                continue
            for n in nodes:
                if self._is_sleep(n) and id(n) not in self._flagged:
                    self._flagged.add(id(n))
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
    """Scan a single Python source *text* for P060 findings."""
    if file.replace("\\", "/").endswith(_HELPER_SUFFIX):
        return []
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    modules, bare = _collect_sleep_aliases(tree)
    checker = _LadderChecker(file, _parse_directives(text), modules, bare)
    checker.check(tree)
    return checker.findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P060 findings."""
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
    description="P060: flag hand-rolled retry ladders (loop + try/except + sleep).",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())

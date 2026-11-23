"""P067 TempDirOutsideRunScratch — a scratch path from ``tempfile``'s system temp dir.

``tempfile.gettempdir()`` (and ``mkdtemp`` / ``mkstemp`` with no ``dir=``) put
a run's files in the system temp dir.  In workflow code the sandbox refuses
``tempfile`` outright; anywhere, ``cleanup_files`` never removes what lands
there, so it stays on the worker's disk after the run; and a ``/tmp/...``
path has no object-store mapping.  ``App.run_scratch_dir`` is the per-run
directory the SDK owns and removes.

The detector flags, resolving each name in the call's own scope (a parameter
or local that shadows the import is not ``tempfile``):

* every call to ``tempfile.gettempdir`` / ``gettempdirb``;
* a call to ``tempfile.mkdtemp`` / ``mkstemp`` that passes no directory —
  neither ``dir=`` nor a third positional argument — or passes ``None`` for
  it, which means the same system temp dir.

A call that splats ``*args`` or ``**kwargs`` is not flagged, since the
directory may be among them.  ``TemporaryDirectory`` / ``NamedTemporaryFile``
are not matched: they remove themselves.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    UNTRUSTED,
    NameResolver,
    _IgnoreDirective,
    make_finding,
    scoped_calls,
)
from conformance.suite.schema.findings import Finding

RULE_ID = "P067"

#: Calls that always name the system temp dir.
_GETTEMPDIR: frozenset[str] = frozenset({"gettempdir", "gettempdirb"})

#: Calls that make a path in the system temp dir unless given a directory;
#: both take it as ``dir=`` or as their third positional argument.
_MAKERS: frozenset[str] = frozenset({"mkdtemp", "mkstemp"})
_DIR_POSITION = 2

_TARGETS = _GETTEMPDIR | _MAKERS

#: Binding kind of a name that is the ``tempfile`` module itself.
_TEMPFILE_MODULE = "tempfile-module"

_MESSAGE = (
    "`{name}(...)` puts run files in the system temp dir: the workflow sandbox "
    "refuses `tempfile` in workflow code, `cleanup_files` never removes the "
    "directory, and a /tmp path has no object-store mapping. Use the App's "
    "`self.run_scratch_dir` (the same path in `run()` and in every task of the "
    "run, removed when the run ends){maker_hint}; outside an App method, take "
    "the directory as a parameter."
)
_MAKER_HINT = ", or pass `dir=self.run_scratch_dir`"


def _classify_import(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    """Map each name *node* binds to ``tempfile-module``, a target, or other."""
    bound: dict[str, str] = {}
    if isinstance(node, ast.ImportFrom):
        from_tempfile = node.level == 0 and node.module == "tempfile"
        for alias in node.names:
            name = alias.asname or alias.name
            if from_tempfile and alias.name in _TARGETS:
                bound[name] = f"tempfile.{alias.name}"
            else:
                bound[name] = UNTRUSTED
    else:
        for alias in node.names:
            if alias.asname is not None:
                is_tempfile = alias.name == "tempfile"
                bound[alias.asname] = _TEMPFILE_MODULE if is_tempfile else UNTRUSTED
            else:
                root = alias.name.split(".")[0]
                bound[root] = _TEMPFILE_MODULE if root == "tempfile" else UNTRUSTED
    return bound


def _target(call: ast.Call, resolve: NameResolver) -> str | None:
    """The ``tempfile`` function *call* invokes in its own scope, or ``None``."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _TARGETS
        and isinstance(func.value, ast.Name)
        and resolve(func.value.id) == _TEMPFILE_MODULE
    ):
        return f"tempfile.{func.attr}"
    if isinstance(func, ast.Name):
        kind = resolve(func.id)
        if kind is not None and kind.startswith("tempfile.") and kind[9:] in _TARGETS:
            return kind
    return None


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _names_a_dir(call: ast.Call) -> bool:
    """True if *call* passes a directory other than ``None``, or may via a splat."""
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return True
    for kw in call.keywords:
        if kw.arg is None:
            return True
        if kw.arg == "dir":
            return not _is_none(kw.value)
    if len(call.args) > _DIR_POSITION:
        return not _is_none(call.args[_DIR_POSITION])
    return False


def check_p067(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Flag scratch paths taken from ``tempfile``'s system temp dir."""
    findings: list[Finding] = []
    for node, name in scoped_calls(tree, _classify_import, _target):
        is_maker = name.split(".", 1)[1] in _MAKERS
        if is_maker and _names_a_dir(node):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id=RULE_ID,
                node=node,
                message=_MESSAGE.format(
                    name=name, maker_hint=_MAKER_HINT if is_maker else ""
                ),
                directives=directives,
            )
        )
    return findings

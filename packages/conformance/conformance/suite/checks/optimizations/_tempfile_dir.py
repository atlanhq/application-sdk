"""O008 TempFileOutsideWorkerVolume — flag stdlib ``tempfile`` scratch with no ``dir=``.

Given no directory, the ``tempfile`` constructors create their file or
directory in the system temp dir (``$TMPDIR``, else ``/tmp``), not under the
SDK's ``TEMPORARY_PATH``.  The SDK base image sets no ``TMPDIR``, so unless an
app mounts its worker volume at ``/tmp`` that scratch lands on the node's root
disk: worker-volume metrics and disk sizing never see it, and it sits outside
the tree the SDK's local cleanup works in (``upload_file`` deletes only under
``TEMPORARY_PATH``; ``cleanup_files`` removes the run's directory beneath it).

Six constructors are graded — ``mkdtemp``, ``mkstemp``, ``NamedTemporaryFile``,
``TemporaryDirectory``, ``TemporaryFile`` and ``SpooledTemporaryFile`` — and a
call is flagged when its directory is:

* omitted (neither ``dir=`` nor the positional slot is given),
* the literal ``None``, or
* ``tempfile.gettempdir()``, which is the same location spelled out.

``dir`` passed positionally is read at each constructor's real position
(:data:`DIR_POSITION`).  Only stdlib bindings count — ``import tempfile [as t]``
and ``from tempfile import mkdtemp [as x]`` — so an app helper that happens to
share a name is never matched.  Any other ``dir`` expression is compliant, and a
``*args`` / ``**kwargs`` splat that may supply it is left alone.

Known limits (biased toward zero false positives at WARN):

* Out of scope: ``tempfile.mktemp`` (deprecated) and paths assembled by hand
  from ``tempfile.gettempdir()``.
* A process-wide ``tempfile.tempdir`` assignment or a ``TMPDIR`` set in deploy
  config is invisible to a per-file scan, so an app that points either under
  ``TEMPORARY_PATH`` still sees findings.
* Storing the callable (``make = tempfile.mkdtemp; make()``), passing it to
  ``run_in_thread`` / ``functools.partial``, and ``from tempfile import *`` are
  not followed — the O001 limitation.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: 0-based position of the ``dir`` parameter in each graded constructor's
#: signature (CPython 3.11-3.14).  ``tests/test_tempfile_dir.py`` re-derives
#: each one from the running interpreter with ``inspect.signature``, so a
#: signature change fails a test instead of silently misreading a positional.
DIR_POSITION: dict[str, int] = {
    "mkdtemp": 2,
    "mkstemp": 2,
    "TemporaryDirectory": 2,
    "NamedTemporaryFile": 6,
    "TemporaryFile": 6,
    "SpooledTemporaryFile": 7,
}

_TEMPFILE = "tempfile"
_GETTEMPDIR = "gettempdir"

_TAIL = (
    "creates its scratch in the system temp dir ($TMPDIR, else /tmp), outside "
    "TEMPORARY_PATH: the SDK's cleanup never reaches it, and unless /tmp is "
    "itself a mounted volume it lands on the node's root disk, where worker-volume "
    "metrics and disk sizing never see it. Pass dir= a directory under "
    "TEMPORARY_PATH (create it first; tempfile does not)."
)


def collect_tempfile_bindings(tree: ast.AST) -> tuple[frozenset[str], dict[str, str]]:
    """Return ``(module_names, func_names)`` for stdlib ``tempfile`` bindings.

    * ``module_names`` — local names bound to the module (``import tempfile as
      t`` → ``t``).
    * ``func_names`` — local name → stdlib name for the graded constructors and
      ``gettempdir`` (``from tempfile import mkdtemp as mk`` → ``{"mk":
      "mkdtemp"}``).

    Walks the whole module, so function-local imports are covered.
    """
    module_names: set[str] = set()
    func_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _TEMPFILE:
                    module_names.add(alias.asname or _TEMPFILE)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module != _TEMPFILE:
                continue
            for alias in node.names:
                if alias.name in DIR_POSITION or alias.name == _GETTEMPDIR:
                    func_names[alias.asname or alias.name] = alias.name
    return frozenset(module_names), func_names


def _stdlib_name(
    func: ast.expr, module_names: frozenset[str], func_names: dict[str, str]
) -> str | None:
    """The stdlib ``tempfile`` function *func* resolves to, else ``None``."""
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
    ):
        return func.attr
    if isinstance(func, ast.Name):
        return func_names.get(func.id)
    return None


def _is_system_temp(
    expr: ast.expr, module_names: frozenset[str], func_names: dict[str, str]
) -> bool:
    """True for ``None`` or ``tempfile.gettempdir()`` — both mean the system temp dir."""
    if isinstance(expr, ast.Constant):
        return expr.value is None
    return (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and _stdlib_name(expr.func, module_names, func_names) == _GETTEMPDIR
    )


def _dir_problem(
    call: ast.Call,
    position: int,
    module_names: frozenset[str],
    func_names: dict[str, str],
) -> str | None:
    """How *call* falls back to the system temp dir, or ``None`` when it does not."""
    explicit = next((kw for kw in call.keywords if kw.arg == "dir"), None)
    if explicit is not None:
        if _is_system_temp(explicit.value, module_names, func_names):
            return f"dir={ast.unparse(explicit.value)}"
        return None
    if any(kw.arg is None for kw in call.keywords):
        return None  # a **kwargs splat may supply dir
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return None  # a *args splat may supply dir
    if len(call.args) > position:
        arg = call.args[position]
        if _is_system_temp(arg, module_names, func_names):
            return f"dir={ast.unparse(arg)}"
        return None
    return "no dir="


def check_o008(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O008 for stdlib ``tempfile`` constructors that fall back to system temp."""
    module_names, func_names = collect_tempfile_bindings(tree)
    if not module_names and not func_names:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _stdlib_name(node.func, module_names, func_names)
        position = DIR_POSITION.get(name) if name is not None else None
        if position is None:
            continue
        problem = _dir_problem(node, position, module_names, func_names)
        if problem is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="O008",
                node=node,
                message=f"tempfile.{name}() with {problem} {_TAIL}",
                directives=directives,
            )
        )
    return sorted(findings, key=lambda f: (f.line, f.column))

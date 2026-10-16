"""P061 TempFileOutsideWorkerVolume — stdlib ``tempfile`` scratch with no ``dir=``.

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
* the literal ``None``,
* ``tempfile.gettempdir()``, the same location spelled out, or
* the literal ``"/tmp"`` / ``"/var/tmp"`` itself (with or without a trailing
  slash) — again the system temp dir, so the cheapest non-fix cannot clear the
  finding.  A sub-directory literal (``"/tmp/output"``) is accepted: some apps
  mount their volume there.

``dir`` passed positionally is read at each constructor's real position
(:data:`DIR_POSITION`).  Only stdlib bindings count, so an app helper that
shares a name is never matched.  Any other ``dir`` expression is accepted, and a
``*args`` / ``**kwargs`` splat that may supply it is left alone.

Known limits (biased toward zero false positives at BLOCK):

* Out of scope: ``tempfile.mktemp`` (deprecated) and paths assembled by hand
  from ``tempfile.gettempdir()``.
* A process-wide ``tempfile.tempdir`` assignment or a ``TMPDIR`` set in deploy
  config is invisible to a per-file scan.
* Storing the callable (``make = tempfile.mkdtemp; make()``), passing it to
  ``run_in_thread`` / ``functools.partial``, and ``from tempfile import *`` are
  not followed — the O001 limitation.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._local_files import TempfileBindings, runs_on_worker
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

_GETTEMPDIR = "gettempdir"

#: Literal directories that are the system temp dir itself.
_SYSTEM_TEMP_LITERALS = frozenset({"/tmp", "/tmp/", "/var/tmp", "/var/tmp/"})

_TAIL = (
    "creates its scratch in the system temp dir ($TMPDIR, else /tmp), outside "
    "TEMPORARY_PATH: the SDK's cleanup never reaches it, and unless /tmp is "
    "itself a mounted volume it lands on the node's root disk, where worker-volume "
    "metrics and disk sizing never see it. Pass dir= a directory under "
    "TEMPORARY_PATH (create it first; tempfile does not)."
)


def _is_system_temp(expr: ast.expr, bindings: TempfileBindings) -> bool:
    """True for ``None``, ``"/tmp"``-like literals and ``tempfile.gettempdir()``."""
    if isinstance(expr, ast.Constant):
        return expr.value is None or expr.value in _SYSTEM_TEMP_LITERALS
    return (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and bindings.resolve(expr.func) == _GETTEMPDIR
    )


def _dir_problem(
    call: ast.Call, position: int, bindings: TempfileBindings
) -> str | None:
    """How *call* falls back to the system temp dir, or ``None`` when it does not."""
    explicit = next((kw for kw in call.keywords if kw.arg == "dir"), None)
    if explicit is not None:
        if _is_system_temp(explicit.value, bindings):
            return f"dir={ast.unparse(explicit.value)}"
        return None
    if any(kw.arg is None for kw in call.keywords):
        return None  # a **kwargs splat may supply dir
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return None  # a *args splat may supply dir
    if len(call.args) > position:
        arg = call.args[position]
        if _is_system_temp(arg, bindings):
            return f"dir={ast.unparse(arg)}"
        return None
    return "no dir="


def check_p061(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P061 for stdlib ``tempfile`` constructors that fall back to system temp."""
    if not runs_on_worker(filename):
        return []
    bindings = TempfileBindings(tree)
    if bindings.empty:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = bindings.resolve(node.func)
        position = DIR_POSITION.get(name) if name is not None else None
        if position is None:
            continue
        problem = _dir_problem(node, position, bindings)
        if problem is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P061",
                node=node,
                message=f"tempfile.{name}() with {problem} {_TAIL}",
                directives=directives,
            )
        )
    return sorted(findings, key=lambda f: (f.line, f.column))

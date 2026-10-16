"""O007 UploadRetainsLocalCopy — flag SDK ``upload_file`` calls that keep the file.

``application_sdk.storage.ops.upload_file`` keeps the local file after a
successful upload unless the caller passes ``retain_local_copy=False`` (the
keyword defaults to ``True``), and even then it deletes only a file that sits
under ``TEMPORARY_PATH``.  A connector that uploads as it goes therefore keeps
every file it has already uploaded on the worker's disk for the rest of the run.

Only calls that resolve to the SDK function are flagged.  Resolution follows the
import binding, the approach O001 takes for stdlib ``json``, so an app's own
``upload_file`` helper, a client method of that name, or the v2
``ObjectStore.upload_file`` classmethod is never matched.  The function is
reachable from every SDK module whose namespace binds it
(:data:`SDK_UPLOAD_MODULES`), each recognised in every import form:

* ``application_sdk.storage``      — the public re-export;
* ``application_sdk.storage.ops``  — where it is defined;
* ``application_sdk.common.incremental.marker`` — a deprecated alias with the
  same signature and default;
* the modules that import it at module level and so re-export it by accident
  (``storage.batch``, ``storage.cloud``, ``common.incremental.state.store``,
  ``testing.harness.seed``).

``tests/test_upload_retain.py`` re-derives that list from the installed SDK, so
a new re-export fails a test instead of going unmatched.

``from M import upload_file [as x]`` binds the function; ``import M as x`` and
``from <package> import <module>`` bind a module whose ``.upload_file(...)`` is
the function; a plain ``import application_sdk...`` lets the fully dotted
``application_sdk.storage.ops.upload_file(...)`` resolve.

A call is flagged when ``retain_local_copy`` is omitted or passed as the literal
``True``.  ``retain_local_copy=False`` is compliant, and any other expression (a
variable, a parameter, a conditional) is left alone because its value is not
known statically.  A ``**kwargs`` splat may carry the keyword, so a call with
one is not flagged either.  The keyword is keyword-only in the SDK signature, so
no positional argument can set it.

Known limits (biased toward zero false positives at WARN):

* Rebinding is not tracked (``up = upload_file; up(...)``), nor is a local name
  that shadows an imported module alias — the O001 limitation.
* Whether a ``retain_local_copy=False`` call's file actually sits under
  ``TEMPORARY_PATH`` is not checked: the path is a runtime value.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: SDK modules from which ``upload_file`` resolves to the SDK's streaming upload:
#: the definition, the public re-export, the deprecated alias, and every module
#: that imports it at module level.
SDK_UPLOAD_MODULES: frozenset[str] = frozenset(
    {
        "application_sdk.storage",
        "application_sdk.storage.ops",
        "application_sdk.storage.batch",
        "application_sdk.storage.cloud",
        "application_sdk.common.incremental.marker",
        "application_sdk.common.incremental.state.store",
        "application_sdk.testing.harness.seed",
    }
)

_FUNC = "upload_file"
_KWARG = "retain_local_copy"
_SDK_ROOT = "application_sdk"

_TAIL = (
    "so the file stays on the worker's disk and a long run piles up every file "
    "it has uploaded. If nothing reads the file afterwards, pass "
    "retain_local_copy=False with the file under TEMPORARY_PATH (the SDK deletes "
    "nothing outside it), or delete the file right after the upload."
)


def collect_upload_bindings(
    tree: ast.AST,
) -> tuple[frozenset[str], frozenset[str], bool]:
    """Return ``(func_names, module_names, sdk_root_bound)`` for SDK ``upload_file``.

    * ``func_names`` — local names bound to the SDK function
      (``from application_sdk.storage import upload_file as su`` → ``su``).
    * ``module_names`` — local names bound to an SDK module that exports it
      (``import application_sdk.storage.ops as ops`` → ``ops``;
      ``from application_sdk import storage`` → ``storage``).
    * ``sdk_root_bound`` — a plain ``import application_sdk[...]`` binds the
      package root, so a fully dotted call path can resolve.

    Walks the whole module, so function-local imports are covered.  Relative
    imports are skipped: an app never reaches the SDK through one.
    """
    func_names: set[str] = set()
    module_names: set[str] = set()
    sdk_root_bound = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module in SDK_UPLOAD_MODULES and alias.name == _FUNC:
                    func_names.add(bound)
                elif f"{node.module}.{alias.name}" in SDK_UPLOAD_MODULES:
                    module_names.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None:
                    if alias.name in SDK_UPLOAD_MODULES:
                        module_names.add(alias.asname)
                elif alias.name == _SDK_ROOT or alias.name.startswith(f"{_SDK_ROOT}."):
                    sdk_root_bound = True
    return frozenset(func_names), frozenset(module_names), sdk_root_bound


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a pure Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _resolves_to_sdk_upload(
    func: ast.expr,
    func_names: frozenset[str],
    module_names: frozenset[str],
    sdk_root_bound: bool,
) -> bool:
    """True if the call target *func* is the SDK's ``upload_file``."""
    if isinstance(func, ast.Name):
        return func.id in func_names
    if not isinstance(func, ast.Attribute) or func.attr != _FUNC:
        return False
    if isinstance(func.value, ast.Name) and func.value.id in module_names:
        return True
    return sdk_root_bound and _dotted(func.value) in SDK_UPLOAD_MODULES


def _retain_problem(call: ast.Call) -> str | None:
    """``"omitted"`` / ``"literal-true"`` when the call keeps the file, else ``None``."""
    explicit = next((kw for kw in call.keywords if kw.arg == _KWARG), None)
    if explicit is not None:
        value = explicit.value
        if isinstance(value, ast.Constant) and value.value is True:
            return "literal-true"
        # Literal False, or an expression whose value is not known statically.
        return None
    if any(kw.arg is None for kw in call.keywords):
        return None  # a **kwargs splat may set it
    return "omitted"


def _message(call: ast.Call, problem: str) -> str:
    written = ast.unparse(call.func)
    name = (
        f"{written}()"
        if written.endswith(_FUNC)
        else f"{written}() (the SDK's upload_file)"
    )
    if problem == "literal-true":
        return (
            f"{name} passes retain_local_copy=True, which keeps the local file, {_TAIL}"
        )
    return (
        f"{name} keeps the local file after the upload "
        f"(retain_local_copy defaults to True), {_TAIL}"
    )


def check_o007(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit O007 for SDK ``upload_file`` calls that keep the local file."""
    func_names, module_names, sdk_root_bound = collect_upload_bindings(tree)
    if not func_names and not module_names and not sdk_root_bound:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _resolves_to_sdk_upload(
            node.func, func_names, module_names, sdk_root_bound
        ):
            continue
        problem = _retain_problem(node)
        if problem is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="O007",
                node=node,
                message=_message(node, problem),
                directives=directives,
            )
        )
    return sorted(findings, key=lambda f: (f.line, f.column))

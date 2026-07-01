"""S002 RawEnvCredentialAccess — flag credential-named ``os.environ`` reads.

Application code that reads a credential-named environment variable directly —
``os.getenv("...SECRET")``, ``os.environ["...TOKEN"]``,
``os.environ.get("...API_KEY")`` — bypasses the SDK secret-store seam.  Only the
env-var *name* (a string literal) is classified; dynamic keys are left alone.
Environment *writes* (``os.environ[x] = v``) are never flagged — only reads.

App-scoped: the SDK is the provider of the seam (``EnvironmentSecretStore``
legitimately reads ``os.environ``), so the runner skips this rule on the SDK.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._secret_names import is_credential_value_name

_MESSAGE = (
    "Credential '{name}' read directly from the environment. Resolve secrets through "
    "the SDK secret store (context.resolve_credential / a CredentialRef, or the "
    "SecretStore protocol) so credential handling stays uniform and auditable. "
    "Suppress a reviewed exception with '# conformance: ignore[S002] <reason>'."
)


def _collect_os_aliases(
    tree: ast.AST,
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(os_module_names, environ_names, getenv_names)`` bound from ``os``.

    * *os_module_names* — names bound to the ``os`` module (``import os [as o]``);
      always includes ``"os"``.
    * *environ_names* — names bound to ``os.environ`` via ``from os import environ``.
    * *getenv_names* — names bound to ``os.getenv`` via ``from os import getenv``.
    """
    os_module_names: set[str] = {"os"}
    environ_names: set[str] = set()
    getenv_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_module_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_names.add(alias.asname or "environ")
                elif alias.name == "getenv":
                    getenv_names.add(alias.asname or "getenv")
    return os_module_names, environ_names, getenv_names


def _string_literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def check_s002(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit S002 for credential-named environment reads in *tree*."""
    os_names, environ_names, getenv_names = _collect_os_aliases(tree)
    findings: list[Finding] = []

    def _is_environ_expr(node: ast.expr) -> bool:
        # ``environ`` (from-import) or ``os.environ`` (attribute on the os module)
        if isinstance(node, ast.Name):
            return node.id in environ_names
        if isinstance(node, ast.Attribute):
            return node.attr == "environ" and (
                isinstance(node.value, ast.Name) and node.value.id in os_names
            )
        return False

    def _emit(name: str, node: ast.AST) -> None:
        if is_credential_value_name(name):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="S002",
                    node=node,
                    message=_MESSAGE.format(name=name),
                    directives=directives,
                )
            )

    for node in ast.walk(tree):
        # os.environ["NAME"] / environ["NAME"] — reads only (skip assignment targets)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if _is_environ_expr(node.value):
                name = _string_literal(node.slice)
                if name is not None:
                    _emit(name, node)
            continue

        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # os.getenv("NAME") / getenv("NAME")
        if isinstance(func, ast.Name) and func.id in getenv_names:
            name = _string_literal(node.args[0]) if node.args else None
            if name is not None:
                _emit(name, node)
        elif isinstance(func, ast.Attribute):
            # os.getenv("NAME")
            if func.attr == "getenv" and (
                isinstance(func.value, ast.Name) and func.value.id in os_names
            ):
                name = _string_literal(node.args[0]) if node.args else None
                if name is not None:
                    _emit(name, node)
            # os.environ.get("NAME") / environ.get("NAME")
            elif func.attr == "get" and _is_environ_expr(func.value):
                name = _string_literal(node.args[0]) if node.args else None
                if name is not None:
                    _emit(name, node)

    return findings

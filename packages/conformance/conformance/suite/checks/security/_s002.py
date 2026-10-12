"""S002 RawEnvCredentialAccess — flag credential-named ``os.environ`` reads.

Application code that reads a credential-named environment variable directly —
``os.getenv("...SECRET")``, ``os.environ["...TOKEN"]``,
``os.environ.get("...API_KEY")``, ``os.environ.pop("...API_KEY")`` — bypasses the
SDK secret-store seam.  Only the env-var *name* (a string literal) is classified;
dynamic keys are left alone.  Environment *writes* (``os.environ[x] = v``) are
never flagged — only reads (``pop`` still returns and exposes the value, so it
counts as a read for this rule).

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


_DEV_SCOPE_TYPES = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)
_NESTED_SCOPE_TYPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


def _imported_symbol_names(
    tree: ast.AST,
    scope: ast.AST,
    parents: dict[ast.AST, ast.AST],
    module: str,
    symbol: str,
) -> set[str]:
    """Return locally visible names imported for *symbol* from *module*."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and _containing_scope(node, parents) in {tree, scope}
        for alias in node.names
        if alias.name == symbol
    }


def _parents_by_node(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _is_within(
    node: ast.AST, ancestor: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    current = node
    while True:
        if current is ancestor:
            return True
        if current not in parents:
            return False
        current = parents[current]


def _containing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, _DEV_SCOPE_TYPES):
            return current
    return None


def _nodes_in_scope(scope: ast.AST) -> list[ast.AST]:
    """Walk one lexical scope without walking into nested functions or classes."""
    nodes: list[ast.AST] = []
    pending = [scope]
    while pending:
        node = pending.pop()
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if child is not scope and isinstance(child, _NESTED_SCOPE_TYPES):
                # Keep the definition node (it binds a name here), not its body.
                nodes.append(child)
            else:
                pending.append(child)
    return nodes


def _simple_name_assignment(node: ast.AST) -> tuple[str, ast.expr] | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        return node.target.id, node.value
    return None


def _name_assignments(
    nodes: list[ast.AST], name: str
) -> list[tuple[ast.AST, ast.expr]]:
    return [
        (node, binding[1])
        for node in nodes
        if (binding := _simple_name_assignment(node)) is not None and binding[0] == name
    ]


def _mapping_writes(
    nodes: list[ast.AST], name: str
) -> list[tuple[ast.AST, ast.Subscript, ast.expr]]:
    writes: list[tuple[ast.AST, ast.Subscript, ast.expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.ctx, ast.Store)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
            ):
                writes.append((node, target, value))
    return writes


def _scope_shadows_import(
    scope: ast.AST,
    nodes: list[ast.AST],
    name: str,
    module: str,
    symbol: str,
) -> bool:
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = scope.args
        if any(
            arg.arg == name
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        ):
            return True
        if arguments.vararg is not None and arguments.vararg.arg == name:
            return True
        if arguments.kwarg is not None and arguments.kwarg.arg == name:
            return True

    for node in nodes:
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Store)
        ):
            return True
        if (
            node is not scope
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local_name = alias.asname or alias.name
                if local_name == name and not (
                    node.module == module and alias.name == symbol
                ):
                    return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                if local_name == name:
                    return True
    return False


def _is_named_call(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in names
    )


def _is_serialized_secret_value(
    node: ast.AST, payload: ast.expr, parents: dict[ast.AST, ast.AST]
) -> bool:
    """Check that a value is serialized as inert JSON data for the mock store."""
    current = node
    saw_serializer = False
    while current is not payload:
        parent = parents.get(current)
        if isinstance(parent, ast.Dict):
            if current not in parent.values:
                return False
            current = parent
            continue
        if isinstance(parent, (ast.List, ast.Tuple)):
            if current not in parent.elts:
                return False
            current = parent
            continue
        if isinstance(parent, ast.Call) and (
            isinstance(parent.func, ast.Attribute)
            and parent.func.attr == "dumps"
            and isinstance(parent.func.value, ast.Name)
            and parent.func.value.id in {"json", "orjson"}
            and len(parent.args) == 1
            and current is parent.args[0]
            and not parent.keywords
        ):
            saw_serializer = True
            current = parent
            continue
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr == "decode"
            and parent.value is current
        ):
            decode_call = parents.get(parent)
            if (
                isinstance(decode_call, ast.Call)
                and decode_call.func is parent
                and not decode_call.args
                and not decode_call.keywords
            ):
                current = decode_call
                continue
        return False
    return saw_serializer


def _is_dev_secret_bootstrap(
    candidate: ast.AST,
    runner: ast.Call,
    nodes: list[ast.AST],
    parents: dict[ast.AST, ast.AST],
    mock_store_names: set[str],
) -> bool:
    """Prove a credential read only provisions run_dev_combined's MockSecretStore.

    This is deliberately a data-flow check, not a filename exclusion: the source
    value must flow through a local secret mapping into MockSecretStore, and that
    store must be passed as ``credential_stores`` to the SDK dev runner. Any
    additional use of the value or either mapping keeps the S002 finding.
    """
    store_keywords = [
        keyword for keyword in runner.keywords if keyword.arg == "credential_stores"
    ]
    if len(store_keywords) != 1 or not isinstance(store_keywords[0].value, ast.Name):
        return False
    stores_name = store_keywords[0].value.id
    stores_definitions = _name_assignments(nodes, stores_name)
    if len(stores_definitions) != 1:
        return False
    stores_assignment, stores_value = stores_definitions[0]
    if (
        not isinstance(stores_value, ast.Dict)
        or stores_assignment.lineno >= runner.lineno
    ):
        return False

    store_loads = [
        node
        for node in nodes
        if isinstance(node, ast.Name)
        and node.id == stores_name
        and isinstance(node.ctx, ast.Load)
    ]
    if len(store_loads) != 1 or store_loads[0] is not store_keywords[0].value:
        return False

    mock_calls = [
        node
        for node in ast.walk(stores_value)
        if _is_named_call(node, mock_store_names)
    ]
    if len(mock_calls) != 1:
        return False
    mock_call = mock_calls[0]
    if len(mock_call.args) == 1 and not mock_call.keywords:
        secret_argument = mock_call.args[0]
    elif not mock_call.args and len(mock_call.keywords) == 1:
        keyword = mock_call.keywords[0]
        if keyword.arg != "secrets":
            return False
        secret_argument = keyword.value
    else:
        return False
    if not isinstance(secret_argument, ast.Name):
        return False

    secrets_name = secret_argument.id
    secret_definitions = _name_assignments(nodes, secrets_name)
    if len(secret_definitions) != 1:
        return False
    secrets_assignment, secrets_value = secret_definitions[0]
    if (
        not isinstance(secrets_value, ast.Dict)
        or secrets_value.keys
        or secrets_value.values
        or secrets_assignment.lineno >= stores_assignment.lineno
    ):
        return False

    secret_writes = _mapping_writes(nodes, secrets_name)
    if not secret_writes or any(
        write.lineno <= secrets_assignment.lineno
        or write.lineno >= stores_assignment.lineno
        for write, _, _ in secret_writes
    ):
        return False

    write_targets = {target for _, target, _ in secret_writes}
    secret_loads = [
        node
        for node in nodes
        if isinstance(node, ast.Name)
        and node.id == secrets_name
        and isinstance(node.ctx, ast.Load)
    ]
    for load in secret_loads:
        if load is secret_argument:
            continue
        parent = parents.get(load)
        if (
            isinstance(parent, ast.Subscript)
            and parent in write_targets
            and parent.value is load
        ):
            continue
        return False

    source_assignments = [
        (node, binding[0], binding[1])
        for node in nodes
        if (binding := _simple_name_assignment(node)) is not None
        and _is_within(candidate, binding[1], parents)
    ]
    if not source_assignments:
        # A direct read inside a secret payload has no reusable local value to
        # escape the store. Do not license a walrus-bound value here.
        inside_named_expression = False
        current = candidate
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.NamedExpr):
                inside_named_expression = True
                break
        return not inside_named_expression and any(
            _is_serialized_secret_value(candidate, value, parents)
            for _, _, value in secret_writes
        )
    if len(source_assignments) != 1:
        return False

    source_assignment, source_name, _ = source_assignments[0]
    source_definitions = _name_assignments(nodes, source_name)
    if (
        len(source_definitions) != 1
        or source_definitions[0][0] is not source_assignment
    ):
        return False

    if source_assignment.lineno >= min(write.lineno for write, _, _ in secret_writes):
        return False

    source_loads = [
        node
        for node in nodes
        if isinstance(node, ast.Name)
        and node.id == source_name
        and isinstance(node.ctx, ast.Load)
    ]
    has_payload_use = False
    for load in source_loads:
        in_secret_payload = any(
            _is_within(load, value, parents) for _, _, value in secret_writes
        )
        if in_secret_payload:
            if not any(
                _is_serialized_secret_value(load, value, parents)
                for _, _, value in secret_writes
            ):
                return False
            has_payload_use = True
            continue
        parent = parents.get(load)
        if isinstance(parent, ast.If) and parent.test is load:
            continue
        return False
    return has_payload_use


def _collect_dev_bootstrap_reads(
    tree: ast.AST, credential_reads: set[ast.AST]
) -> set[ast.AST]:
    """Find only env reads proven to feed the SDK's local dev secret store."""
    if not credential_reads:
        return set()

    parents = _parents_by_node(tree)
    module_nodes = _nodes_in_scope(tree)
    scopes: dict[ast.AST, list[ast.AST]] = {}
    exempt: set[ast.AST] = set()
    for candidate in credential_reads:
        scope = _containing_scope(candidate, parents)
        if scope is None:
            continue
        if scope not in scopes:
            scopes[scope] = _nodes_in_scope(scope)
        nodes = scopes[scope]
        run_dev_names = _imported_symbol_names(
            tree, scope, parents, "application_sdk.main", "run_dev_combined"
        )
        mock_store_names = _imported_symbol_names(
            tree,
            scope,
            parents,
            "application_sdk.testing.mocks",
            "MockSecretStore",
        )
        if not run_dev_names or not mock_store_names:
            continue

        shadowed_run_names = any(
            _scope_shadows_import(
                lexical_scope,
                lexical_nodes,
                name,
                "application_sdk.main",
                "run_dev_combined",
            )
            for name in run_dev_names
            for lexical_scope, lexical_nodes in ((tree, module_nodes), (scope, nodes))
        )
        shadowed_store_names = any(
            _scope_shadows_import(
                lexical_scope,
                lexical_nodes,
                name,
                "application_sdk.testing.mocks",
                "MockSecretStore",
            )
            for name in mock_store_names
            for lexical_scope, lexical_nodes in ((tree, module_nodes), (scope, nodes))
        )
        if shadowed_run_names or shadowed_store_names:
            continue

        for node in nodes:
            if not _is_named_call(node, run_dev_names):
                continue
            if _is_dev_secret_bootstrap(
                candidate,
                node,
                nodes,
                parents,
                mock_store_names,
            ):
                exempt.add(candidate)
                break
    return exempt


def check_s002(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit S002 for credential-named environment reads in *tree*."""
    os_names, environ_names, getenv_names = _collect_os_aliases(tree)
    findings: list[Finding] = []
    reads: list[tuple[str, ast.AST]] = []

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
                if name is not None and is_credential_value_name(name):
                    reads.append((name, node))
            continue

        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # os.getenv("NAME") / getenv("NAME")
        if isinstance(func, ast.Name) and func.id in getenv_names:
            name = _string_literal(node.args[0]) if node.args else None
            if name is not None and is_credential_value_name(name):
                reads.append((name, node))
        elif isinstance(func, ast.Attribute) and (
            (
                func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id in os_names
            )
            or (func.attr in {"get", "pop"} and _is_environ_expr(func.value))
        ):
            # os.getenv("NAME") / os.environ.get("NAME") / os.environ.pop("NAME")
            name = _string_literal(node.args[0]) if node.args else None
            if name is not None and is_credential_value_name(name):
                reads.append((name, node))

    dev_bootstrap_reads = _collect_dev_bootstrap_reads(
        tree, {node for _, node in reads}
    )
    for name, node in reads:
        if node not in dev_bootstrap_reads:
            _emit(name, node)

    return findings

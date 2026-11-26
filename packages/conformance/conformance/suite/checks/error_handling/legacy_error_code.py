"""E022 — a class body binds ``DEFAULT_ERROR_CODE`` in the SDK's legacy ``AAF-*`` vocabulary.

The SDK's one error vocabulary is the ``code`` ClassVar. ``DEFAULT_ERROR_CODE``
was the second, carrying an ``AAF-*`` ``ErrorCode`` beside ``code`` (FND-4299).
The SDK no longer declares it on any class, and its ``AAF-*`` names warn on
access until v4.0.0 removes them.

The rule is an allowlist of two syntactic shapes, both local to the class:

* the class reaches an SDK class that carries an ``AAF-*`` code
  (:data:`SDK_LEGACY_CODED_CLASSES`) through its bases: directly, imported by
  name from ``application_sdk`` or through an ``application_sdk`` module alias,
  or through app-defined intermediates in the same module or imported from the
  scanned repo (the in-repo resolution :mod:`._sdk_errors` and E021 use);
* the bound value or its annotation names the SDK's ``ErrorCode`` or one of its
  ``AAF-*`` constants, imported from ``application_sdk.errors`` or reached as
  ``application_sdk.errors.<NAME>``.

An app's own ``DEFAULT_ERROR_CODE`` on its own mixin, typed with its own code
class, matches neither shape and is not flagged: that is the app's design, not
the SDK's legacy vocabulary.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ._constants import SDK_LEGACY_CODE_NAMES, SDK_LEGACY_CODED_CLASSES
from ._sdk_errors import _Resolver as _SdkErrorResolver

_SDK_PACKAGE = "application_sdk"

_ERRORS_MODULES: frozenset[str] = frozenset(
    {"application_sdk.errors", "application_sdk.errors._legacy_codes"}
)


def _default_error_code_bindings(
    node: ast.ClassDef,
) -> list[ast.Assign | ast.AnnAssign]:
    """Statements in *node*'s own body that bind ``DEFAULT_ERROR_CODE``."""
    found: list[ast.Assign | ast.AnnAssign] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign):
            targets: list[ast.expr] = [stmt.target]
        elif isinstance(stmt, ast.Assign):
            targets = stmt.targets
        else:
            continue
        if any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_ERROR_CODE" for t in targets
        ):
            found.append(stmt)
    return found


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return ".".join(reversed(parts))


def _sdk_legacy_name(module: str, name: str) -> str | None:
    """*name* when it is an SDK legacy-coded class imported from *module*.

    A name from ``application_sdk.errors`` never matches: the canonical
    ``AppError`` there carries no ``AAF-*`` code, only ``application_sdk.app``'s
    deprecated shim of the same name does.
    """
    if module == "application_sdk.errors" or module.startswith(
        "application_sdk.errors."
    ):
        return None
    return name if name in SDK_LEGACY_CODED_CLASSES else None


def _sdk_module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to an ``application_sdk`` module, mapped to that module."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] != _SDK_PACKAGE:
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[_SDK_PACKAGE] = _SDK_PACKAGE
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and node.module.split(".")[0] == _SDK_PACKAGE
        ):
            for alias in node.names:
                if not alias.name[:1].isupper():
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


class _Resolver(_SdkErrorResolver):
    """Reuses :mod:`._sdk_errors`' in-repo module loading to answer: which SDK
    legacy-coded class, if any, does a class reach through its bases?"""

    def name_base(
        self,
        tree: ast.Module,
        path: Path | None,
        name: str,
        visited: frozenset[tuple[Path | None, str]],
    ) -> str | None:
        key = (path, name)
        if key in visited:
            return None
        visited = visited | {key}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                if (
                    node.level == 0
                    and node.module is not None
                    and (node.module.split(".")[0] == _SDK_PACKAGE)
                ):
                    return _sdk_legacy_name(node.module, alias.name)
                target = self.module_file(node.module, node.level, path)
                loaded = self.load(target) if target is not None else None
                if loaded is None:
                    return None
                return self.name_base(loaded, target, alias.name, visited)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return self.class_base(tree, path, node, visited)
        return None

    def class_base(
        self,
        tree: ast.Module,
        path: Path | None,
        node: ast.ClassDef,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> str | None:
        for base in node.bases:
            if isinstance(base, ast.Name):
                found = self.name_base(tree, path, base.id, visited)
            else:
                dotted = _dotted(base)
                found = None
                if dotted is not None and "." in dotted:
                    head, _, rest = dotted.partition(".")
                    module = _sdk_module_aliases(tree).get(head)
                    if module is not None:
                        qualified = f"{module}.{rest}"
                        owner, _, attr = qualified.rpartition(".")
                        found = _sdk_legacy_name(owner, attr)
            if found is not None:
                return found
        return None


def legacy_coded_bases(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> dict[str, str]:
    """Each class in *tree* that reaches an SDK legacy-coded class, mapped to it.

    Without *root*, bases imported from the scanned repo cannot be followed;
    same-module intermediates and SDK imports still resolve.
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base = resolver.class_base(tree, module_path, node)
            if base is not None:
                found.setdefault(node.name, base)
    return found


def legacy_coded_names(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> dict[str, str]:
    """Each module-level name in *tree* that resolves to an SDK legacy-coded class.

    E023's view: the classes :func:`legacy_coded_bases` finds, plus every name a
    module-level ``from … import`` binds that reaches one, whether it is the SDK
    class itself (``from application_sdk.storage.errors import StorageError``)
    or an app subclass imported from the scanned repo.
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    found = dict(legacy_coded_bases(tree, path=path, root=root))
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            local = alias.asname or alias.name
            if local in found:
                continue
            base = resolver.name_base(tree, module_path, local, frozenset())
            if base is not None:
                found[local] = base
    found.update(_qualified_repo_names(tree, resolver, module_path))
    return found


def _repo_module_refs(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """Module-level names that may bind a module of the scanned repo.

    Each maps to the ``(module, level)`` :meth:`_Resolver.module_file` takes:
    ``import app.errors as errors`` binds ``errors`` to ``app.errors``,
    ``import app.errors`` binds ``app``, and ``from app import errors`` or
    ``from . import errors`` binds ``errors`` to that submodule. SDK imports
    are left out: :meth:`LegacyErrorCodeMixin._sdk_origin` resolves those.
    """
    refs: dict[str, tuple[str, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _SDK_PACKAGE:
                    continue
                if alias.asname:
                    refs[alias.asname] = (alias.name, 0)
                else:
                    head = alias.name.split(".")[0]
                    refs[head] = (head, 0)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (
                node.module is None or node.module.split(".")[0] == _SDK_PACKAGE
            ):
                continue
            for alias in node.names:
                module = f"{node.module}.{alias.name}" if node.module else alias.name
                refs[alias.asname or alias.name] = (module, node.level)
    return refs


def _qualified_repo_names(
    tree: ast.Module, resolver: _Resolver, module_path: Path | None
) -> dict[str, str]:
    """Each ``mod.Cls`` chain in *tree* naming a repo class that reaches an SDK
    legacy-coded class, keyed by the dotted text (``errors.AppStorageError``).

    Whether a given use's head still names that module is decided per scope
    at the use (:meth:`LegacyErrorCodeMixin._scope_binding`).
    """
    refs = _repo_module_refs(tree)
    if not refs:
        return {}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        dotted = _dotted(node)
        if dotted is None or dotted in found:
            continue
        parts = dotted.split(".")
        ref = refs.get(parts[0])
        if ref is None:
            continue
        module, level = ref
        owner = ".".join([module, *parts[1:-1]])
        target = resolver.module_file(owner, level, module_path)
        loaded = resolver.load(target) if target is not None else None
        if loaded is None:
            continue
        base = resolver.name_base(loaded, target, parts[-1], frozenset())
        if base is not None:
            found[dotted] = base
    return found


_SELF_NAMES: frozenset[str] = frozenset({"self", "cls"})

_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef

#: Nodes that open their own scope: their name (if any) binds in the
#: enclosing scope, but names bound inside them do not.
_SCOPE_BOUNDARIES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

#: A name bound in a scope, mapped to the SDK legacy-coded class it names
#: there, or ``None`` when that binding is anything else (shadowing).
_ScopeBindings = dict[str, "str | None"]


def _scope_nodes(body: list[ast.stmt]) -> list[ast.AST]:
    """The nodes of one scope: *body* without the inside of nested scopes."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPE_BOUNDARIES):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _shadowing_names(node: ast.AST, *, module_scope: bool) -> list[str]:
    """Names *node* binds in its scope other than by a ``from`` import."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        return [node.id]
    if isinstance(node, _FuncDef):
        return [node.name]
    if isinstance(node, ast.ClassDef):
        return [] if module_scope else [node.name]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    if isinstance(node, ast.Import) and not module_scope:
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    return []


def _from_import_bindings(node: ast.ImportFrom) -> _ScopeBindings:
    """What a function-local ``from`` import binds: an SDK legacy-coded class
    keeps resolving to it, anything else shadows."""
    from_sdk = (
        node.level == 0
        and node.module is not None
        and node.module.split(".")[0] == _SDK_PACKAGE
    )
    return {
        alias.asname or alias.name: (
            _sdk_legacy_name(node.module, alias.name)
            if from_sdk and node.module is not None
            else None
        )
        for alias in node.names
    }


def _bindings_of(
    nodes: list[ast.AST], *, module_scope: bool
) -> tuple[_ScopeBindings, set[str]]:
    """Names *nodes* bind, and the names they declare ``global``.

    At module scope imports and class definitions are what
    :func:`legacy_coded_names` already resolved, so only the other bindings
    (an assignment, a ``def``, a handler name) are returned.
    """
    bound: _ScopeBindings = {}
    declared_global: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.ImportFrom) and not module_scope:
            bound.update(_from_import_bindings(node))
        else:
            for name in _shadowing_names(node, module_scope=module_scope):
                bound.setdefault(name, None)
    return bound, declared_global


def _function_bindings(node: _FuncDef) -> tuple[_ScopeBindings, set[str]]:
    """Names *node*'s own scope binds (parameters included), minus globals."""
    bound, declared_global = _bindings_of(_scope_nodes(node.body), module_scope=False)
    args = node.args
    for arg in [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        *([args.vararg] if args.vararg else []),
        *([args.kwarg] if args.kwarg else []),
    ]:
        bound[arg.arg] = None
    for name in declared_global:
        bound.pop(name, None)
    return bound, declared_global


def _module_rebindings(tree: ast.Module) -> frozenset[str]:
    """Module-scope names bound other than by import or class definition,
    including through a function's ``global`` declaration."""
    bound, _ = _bindings_of(_scope_nodes(tree.body), module_scope=True)
    names = set(bound)
    for node in ast.walk(tree):
        if isinstance(node, _FuncDef):
            stored, declared_global = _bindings_of(
                _scope_nodes(node.body), module_scope=False
            )
            names.update(declared_global & set(stored))
    return frozenset(names)


class LegacyErrorCodeMixin:
    """Rule methods for E022 and E023 (legacy-error-code)."""

    _import_origins: dict[str, str]
    _legacy_coded_bases: dict[str, str]
    _legacy_coded_names: dict[str, str]
    _function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef]
    _module: ast.Module | None
    # E023 per-scope bindings, keyed by id() of the function node, and the
    # module's own non-import rebindings (computed on first use).
    _e023_scope_cache: dict[int, dict[str, str | None]]
    _e023_module_rebound: frozenset[str] | None
    _class_stack: list[ast.ClassDef]
    _except_stack: list[ast.ExceptHandler]

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None: ...

    def _sdk_origin(self, expr: ast.expr) -> str | None:
        """The ``module.attr`` an expression names through this module's imports."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        origin = self._import_origins.get(head)
        if origin is None:
            return None
        return f"{origin}.{rest}" if rest else origin

    def _names_legacy_vocabulary(self, expr: ast.expr | None) -> bool:
        if expr is None:
            return False
        for sub in ast.walk(expr):
            if not isinstance(sub, ast.Name | ast.Attribute):
                continue
            origin = self._sdk_origin(sub)
            if origin is None:
                continue
            module, _, name = origin.rpartition(".")
            if module in _ERRORS_MODULES and name in SDK_LEGACY_CODE_NAMES:
                return True
        return False

    # ── E022 ─────────────────────────────────────────────────────────────────

    def _check_e022(self, node: ast.ClassDef) -> None:
        bindings = _default_error_code_bindings(node)
        if not bindings:
            return
        base = self._legacy_coded_bases.get(node.name)
        for stmt in bindings:
            annotation = stmt.annotation if isinstance(stmt, ast.AnnAssign) else None
            legacy_value = self._names_legacy_vocabulary(
                stmt.value
            ) or self._names_legacy_vocabulary(annotation)
            if base is None and not legacy_value:
                continue
            reason = (
                f"overrides the AAF-* code {node.name} inherits from the SDK's {base}"
                if base is not None
                else "binds it to the SDK's deprecated AAF-* ErrorCode vocabulary"
            )
            self._add(
                "E022",
                stmt,
                f"{node.name} declares DEFAULT_ERROR_CODE: it {reason}. The SDK's "
                f"one error vocabulary is the code ClassVar; AAF-* codes are "
                f"deprecated and removed in v4.0.0. Delete the DEFAULT_ERROR_CODE "
                f"line and give the class its own code ClassVar.",
            )

    # ── E023 ─────────────────────────────────────────────────────────────────

    def _scope_binding(self, name: str) -> tuple[bool, str | None]:
        """Whether a scope at the current use rebinds *name*, and if so the SDK
        legacy-coded class that binding names.

        Python resolves a name in the innermost enclosing function that binds
        it, else at module scope, so only those scopes are consulted: a
        parameter of an unrelated function does not shadow the import here.
        """
        cache = self._e023_scope_cache
        for func in reversed(self._function_stack):
            key = id(func)
            if key not in cache:
                cache[key] = _function_bindings(func)[0]
            if name in cache[key]:
                return True, cache[key][name]
        rebound = self._e023_module_rebound
        if rebound is None:
            rebound = (
                _module_rebindings(self._module)
                if self._module is not None
                else frozenset()
            )
            self._e023_module_rebound = rebound
        return (True, None) if name in rebound else (False, None)

    def _legacy_class_of(self, expr: ast.expr) -> str | None:
        """The SDK legacy-coded class *expr* names, if it names one.

        A name a scope at this use rebinds (a parameter, a local assignment, a
        handler name) means that binding, not the module import it shadows.
        """
        if isinstance(expr, ast.Name):
            shadowed, local = self._scope_binding(expr.id)
            if shadowed:
                return local
            return self._legacy_coded_names.get(expr.id)
        dotted = _dotted(expr)
        if dotted is None or "." not in dotted:
            return None
        if self._scope_binding(dotted.partition(".")[0])[0]:
            return None
        repo_class = self._legacy_coded_names.get(dotted)
        if repo_class is not None:
            return repo_class
        origin = self._sdk_origin(expr)
        if origin is None or origin.split(".")[0] != _SDK_PACKAGE:
            return None
        owner, _, attr = origin.rpartition(".")
        return _sdk_legacy_name(owner, attr)

    def _enclosing_legacy_class(self) -> str | None:
        """The SDK legacy-coded class the innermost enclosing class reaches."""
        if not self._class_stack:
            return None
        return self._legacy_coded_names.get(self._class_stack[-1].name)

    def _handler_legacy_class(self, name: str) -> str | None:
        """The SDK legacy-coded class the innermost ``except … as name`` catches."""
        for handler in reversed(self._except_stack):
            if handler.name != name or handler.type is None:
                continue
            caught = (
                handler.type.elts
                if isinstance(handler.type, ast.Tuple)
                else [handler.type]
            )
            for expr in caught:
                found = self._legacy_class_of(expr)
                if found is not None:
                    return found
            return None
        return None

    def _e023(self, node: ast.AST, what: str, cls: str, fix: str) -> None:
        self._add(
            "E023",
            node,
            f"{what} on {cls}, an SDK error carrying the deprecated AAF-* "
            f"vocabulary. It warns today and is removed in v4.0.0, when this "
            f"line fails. {fix}",
        )

    def _check_e023_call(self, node: ast.Call) -> None:
        keyword = next((k for k in node.keywords if k.arg == "error_code"), None)
        if keyword is None:
            return
        cls = self._legacy_class_of(node.func)
        if cls is None and _is_super_init(node.func):
            cls = self._enclosing_legacy_class()
        if cls is None:
            return
        self._e023(
            keyword.value,
            "Passes the error_code= keyword",
            cls,
            "Drop the keyword and raise a subclass that sets its own code ClassVar.",
        )

    def _check_e023_attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        if node.attr not in {"DEFAULT_ERROR_CODE", "error_code"}:
            return
        value = node.value
        cls: str | None = None
        if isinstance(value, ast.Name) and value.id in _SELF_NAMES:
            cls = self._enclosing_legacy_class()
        elif node.attr == "DEFAULT_ERROR_CODE":
            cls = self._legacy_class_of(value)
        elif isinstance(value, ast.Name):
            cls = self._handler_legacy_class(value.id)
        if cls is None:
            return
        self._e023(
            node,
            f"Reads {node.attr}",
            cls,
            "Read the code ClassVar instead (a str: .error_code.code becomes .code).",
        )


def _is_super_init(func: ast.expr) -> bool:
    """``super().__init__`` or ``super(X, self).__init__``."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "__init__"
        and isinstance(func.value, ast.Call)
        and isinstance(func.value.func, ast.Name)
        and func.value.func.id == "super"
    )

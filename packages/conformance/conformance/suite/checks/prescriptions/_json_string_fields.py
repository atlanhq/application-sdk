"""P059 JsonEncodedContractField — JSON text carried in a ``str`` contract field.

A ``str`` or ``list[str]`` field on an ``Input``/``Output`` contract whose value
the app writes with ``json.dumps`` (or ``model_dump_json``) or reads back with
``json.loads`` (or ``model_validate_json`` / ``TypeAdapter.validate_json``).
The contract says "text"; the data is a structure.  Payload safety (P001) and
the unmodeled-container check (P015) see only a string, so the shape and the
size of the value are hidden from both, and from the contract ledger.

Cross-file (``scan_all`` pass): the field is declared in the contracts module
and encoded or decoded in the task code, so evidence from every scanned file
is matched to the declaration by field name and by the contract class the
receiver is typed as (see :class:`_Evidence`).
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import (
    _container_element_type,
    _terminal_name,
    collect_foreign_contract_names,
    is_str_annotation,
    iter_contract_classes,
    strip_annotation_wrappers,
)

_JSON_MODULES: frozenset[str] = frozenset({"json", "orjson", "ujson", "simplejson"})
_DECODE_METHODS: frozenset[str] = frozenset({"model_validate_json", "validate_json"})
_ENCODE_METHODS: frozenset[str] = frozenset({"model_dump_json"})
_LIST_LIKE_NAMES: frozenset[str] = frozenset(
    {
        "list",
        "List",
        "Sequence",
        "MutableSequence",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
    }
)


def _is_str_node(node: ast.expr) -> bool:
    return is_str_annotation(strip_annotation_wrappers(node))


def _is_text_field(annotation: ast.expr) -> bool:
    node = strip_annotation_wrappers(annotation)
    if _is_str_node(node):
        return True
    if not isinstance(node, ast.Subscript):
        return False
    if _terminal_name(node.value) not in _LIST_LIKE_NAMES:
        return False
    elem = _container_element_type(node)
    return elem is not None and _is_str_node(elem)


def _json_module_aliases(tree: ast.AST) -> frozenset[str]:
    aliases = set(_JSON_MODULES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _JSON_MODULES and alias.asname:
                    aliases.add(alias.asname)
    return frozenset(aliases)


def _json_function_aliases(tree: ast.AST, name: str) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _JSON_MODULES:
            for alias in node.names:
                if alias.name == name:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_json_call(
    node: ast.expr, name: str, bare: set[str], modules: frozenset[str]
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == name:
        return isinstance(func.value, ast.Name) and func.value.id in modules
    return isinstance(func, ast.Name) and func.id in bare


def _is_decode_call(node: ast.expr, loads: set[str], modules: frozenset[str]) -> bool:
    if _is_json_call(node, "loads", loads, modules):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DECODE_METHODS
    )


def _is_encode_value(node: ast.expr, dumps: set[str], modules: frozenset[str]) -> bool:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "decode"
    ):
        node = node.func.value
    if _is_json_call(node, "dumps", dumps, modules):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _ENCODE_METHODS
    )


def _encoded_field_value(
    node: ast.expr, dumps: set[str], modules: frozenset[str]
) -> bool:
    if _is_encode_value(node, dumps, modules):
        return True
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _is_encode_value(node.elt, dumps, modules)
    return False


#: Calls on a contract class that return an instance of it.
_BUILDER_METHODS: frozenset[str] = frozenset(
    {"model_validate", "model_validate_json", "model_construct"}
)
#: Pydantic decorators that name the fields a method rewrites.
_VALIDATOR_DECORATORS: frozenset[str] = frozenset({"field_validator", "validator"})
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

#: ``(field name, contract class the receiver is typed as)``.
_FieldRef = tuple[str, str]


def _contract_of(
    annotation: ast.expr | None, contract_names: frozenset[str]
) -> str | None:
    """The contract class *annotation* names, through wrappers and string quotes."""
    if annotation is None:
        return None
    node = strip_annotation_wrappers(annotation)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = strip_annotation_wrappers(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return None
    name = _terminal_name(node)
    return name if name in contract_names else None


def _constructed_contract(node: ast.expr, contract_names: frozenset[str]) -> str | None:
    """``C(...)`` / ``C.model_validate(...)`` on a contract class → ``"C"``."""
    if not isinstance(node, ast.Call):
        return None
    name = _terminal_name(node.func)
    if name in contract_names:
        return name
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _BUILDER_METHODS:
        owner = _terminal_name(func.value)
        if owner in contract_names:
            return owner
    return None


def _walk_scope(nodes: list[ast.AST]) -> list[ast.AST]:
    """Every node under *nodes*, yielding nested scopes but not descending into them."""
    out: list[ast.AST] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPE_NODES):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _target_names(target: ast.expr) -> list[str]:
    """Names bound by an assignment target (through tuple/list unpacking and ``*rest``)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _target_names(elt)]
    return []


def _is_staticmethod(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_terminal_name(d) == "staticmethod" for d in func.decorator_list)


class _Evidence:
    """Collect JSON encode/decode sites on receivers typed as a contract class.

    A receiver is contract-typed when it is a parameter annotated with a
    contract class, a local bound to ``C(...)`` / ``C.model_validate(...)`` or
    annotated ``C``, or ``self`` / ``cls`` in a method of contract class ``C``.
    An attribute on anything else (an unannotated parameter, an HTTP response,
    a row) is not evidence, however its name matches a contract field.
    """

    def __init__(self, tree: ast.Module, contract_names: frozenset[str]) -> None:
        self.contracts = contract_names
        self.loads = _json_function_aliases(tree, "loads")
        self.dumps = _json_function_aliases(tree, "dumps")
        self.modules = _json_module_aliases(tree)
        self.found: dict[_FieldRef, tuple[str, int]] = {}
        self._scope(tree.body, self._bind({}, tree.body), owner=None)

    def _bindings(self, node: ast.AST) -> list[tuple[str, str | None]]:
        """Each name *node* binds, with the contract class it binds it to (or ``None``)."""
        if isinstance(node, ast.Assign):
            cls = _constructed_contract(node.value, self.contracts)
            return [
                (name, cls if isinstance(target, ast.Name) else None)
                for target in node.targets
                for name in _target_names(target)
            ]
        if isinstance(node, ast.AnnAssign):
            cls = _contract_of(node.annotation, self.contracts)
            if cls is None and node.value is not None:
                cls = _constructed_contract(node.value, self.contracts)
            return [(name, cls) for name in _target_names(node.target)]
        if isinstance(node, ast.NamedExpr):
            cls = _constructed_contract(node.value, self.contracts)
            return [(node.target.id, cls)]
        if isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            return [(name, None) for name in _target_names(node.target)]
        if isinstance(node, ast.withitem) and node.optional_vars is not None:
            return [(name, None) for name in _target_names(node.optional_vars)]
        if isinstance(node, ast.ExceptHandler) and node.name:
            return [(node.name, None)]
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return [((a.asname or a.name).split(".")[0], None) for a in node.names]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [(node.name, None)]
        return []

    def _bind(
        self,
        env: dict[str, str],
        body: list[ast.stmt],
        params: dict[str, str | None] | None = None,
    ) -> dict[str, str]:
        """Type each name bound in *body*; a name keeps a type only if every binding agrees.

        The scan is flow-insensitive, so ``x = Contract(); x = response`` must
        not leave ``x`` typed as ``Contract``: any binding to something else
        (or to a different contract) makes the name's type unknown.
        """
        kinds: dict[str, set[str | None]] = {
            name: {cls} for name, cls in (params or {}).items()
        }
        for node in _walk_scope(list(body)):
            for name, cls in self._bindings(node):
                kinds.setdefault(name, set()).add(cls)
        env = dict(env)
        for name, seen in kinds.items():
            only = next(iter(seen)) if len(seen) == 1 else None
            if only is None:
                env.pop(name, None)
            else:
                env[name] = only
        return env

    def _function_env(
        self,
        func: ast.FunctionDef | ast.AsyncFunctionDef,
        env: dict[str, str],
        owner: str | None,
    ) -> dict[str, str]:
        args = func.args
        positional = [*args.posonlyargs, *args.args]
        params: dict[str, str | None] = {
            arg.arg: _contract_of(arg.annotation, self.contracts)
            for arg in [*positional, *args.kwonlyargs]
        }
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                params[extra.arg] = None
        if owner is not None and positional and not _is_staticmethod(func):
            params[positional[0].arg] = owner
        return self._bind(env, func.body, params)

    def _scope(
        self, body: list[ast.stmt], env: dict[str, str], owner: str | None
    ) -> None:
        for node in _walk_scope(list(body)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if owner is not None:
                    self._inspect_validator(node, owner)
                self._scope(node.body, self._function_env(node, env, owner), None)
            elif isinstance(node, ast.ClassDef):
                cls = node.name if node.name in self.contracts else None
                self._scope(node.body, env, cls)
            elif isinstance(node, ast.Lambda):
                inner = dict(env)
                for arg in [*node.args.posonlyargs, *node.args.args]:
                    inner.pop(arg.arg, None)
                self._scope([ast.Expr(node.body)], inner, None)
            else:
                self._inspect(node, env)

    def _ref(
        self,
        node: ast.expr,
        env: dict[str, str],
        bound: dict[str, _FieldRef] | None = None,
    ) -> _FieldRef | None:
        """Resolve ``x.FIELD``, ``x.FIELD or default`` or ``getattr(x, "FIELD")``."""
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                ref = self._ref(value, env, bound)
                if ref is not None:
                    return ref
            return None
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in env
        ):
            return node.attr, env[node.value.id]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in env
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            return node.args[1].value, env[node.args[0].id]
        if bound is not None and isinstance(node, ast.Name) and node.id in bound:
            return bound[node.id]
        return None

    def _note(self, ref: _FieldRef | None, kind: str, node: ast.AST) -> None:
        if ref is not None:
            self.found.setdefault(ref, (kind, getattr(node, "lineno", 0)))

    def _is_loads(self, fn: ast.expr) -> bool:
        if isinstance(fn, ast.Attribute):
            return (
                fn.attr == "loads"
                and isinstance(fn.value, ast.Name)
                and fn.value.id in self.modules
            )
        return isinstance(fn, ast.Name) and fn.id in self.loads

    def _inspect_validator(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, owner: str
    ) -> None:
        """``@field_validator("a", "b")`` on contract ``owner`` returning ``json.dumps(v)``."""
        fields = [
            arg.value
            for dec in func.decorator_list
            if isinstance(dec, ast.Call)
            and _terminal_name(dec.func) in _VALIDATOR_DECORATORS
            for arg in dec.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if not fields:
            return
        for node in _walk_scope(list(func.body)):
            if (
                isinstance(node, ast.Return)
                and node.value is not None
                and _encoded_field_value(node.value, self.dumps, self.modules)
            ):
                for field in fields:
                    self._note((field, owner), "encoded", node)
                return

    def _inspect(self, node: ast.AST, env: dict[str, str]) -> None:
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            bound: dict[str, _FieldRef] = {}
            for gen in node.generators:
                ref = self._ref(gen.iter, env)
                if ref is not None and isinstance(gen.target, ast.Name):
                    bound[gen.target.id] = ref
            elt = node.elt
            if (
                bound
                and _is_decode_call(elt, self.loads, self.modules)
                and isinstance(elt, ast.Call)
                and elt.args
            ):
                self._note(self._ref(elt.args[0], {}, bound), "decoded", elt)
        if isinstance(node, ast.Call):
            if _is_decode_call(node, self.loads, self.modules) and node.args:
                self._note(self._ref(node.args[0], env), "decoded", node)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "map"
                and len(node.args) == 2
                and self._is_loads(node.args[0])
            ):
                self._note(self._ref(node.args[1], env), "decoded", node)
            cls = _constructed_contract(node, self.contracts)
            if cls is not None:
                for kw in node.keywords:
                    if kw.arg is not None and _encoded_field_value(
                        kw.value, self.dumps, self.modules
                    ):
                        self._note((kw.arg, cls), "encoded", kw.value)
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        value = getattr(node, "value", None)
        if (
            targets
            and isinstance(value, ast.expr)
            and _encoded_field_value(value, self.dumps, self.modules)
        ):
            for target in targets:
                self._note(self._ref(target, env), "encoded", node)


def _lineages(file_trees: dict[Path, ast.AST]) -> dict[str, frozenset[str]]:
    """Map each class name to itself plus every base class it inherits from."""
    parents: dict[str, set[str]] = {}
    for tree in file_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names = {_terminal_name(b) for b in node.bases}
                parents.setdefault(node.name, set()).update(
                    n for n in names if n is not None
                )
    lineages: dict[str, frozenset[str]] = {}
    for name in parents:
        seen = {name}
        stack = [name]
        while stack:
            for base in parents.get(stack.pop(), ()):
                if base not in seen:
                    seen.add(base)
                    stack.append(base)
        lineages[name] = frozenset(seen)
    return lineages


def check_p059(
    file_trees: dict[Path, ast.AST],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P059 for text contract fields whose value is encoded or decoded as JSON."""
    contract_names = frozenset(
        classdef.name
        for tree in file_trees.values()
        for classdef in iter_contract_classes(
            tree, collect_foreign_contract_names(tree)
        )
    )
    lineages = _lineages(file_trees)
    # A receiver typed as a subclass of a contract carries the contract's fields.
    receiver_types = contract_names | frozenset(
        name for name, lineage in lineages.items() if lineage & contract_names
    )
    # field -> [(receiver class, path, kind, line)], first site per receiver class
    evidence: dict[str, list[tuple[str, Path, str, int]]] = {}
    for path, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        for (field, cls), (kind, line) in _Evidence(tree, receiver_types).found.items():
            sites = evidence.setdefault(field, [])
            if all(site[0] != cls for site in sites):
                sites.append((cls, path, kind, line))

    findings: list[Finding] = []
    for path, tree in file_trees.items():
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        foreign = collect_foreign_contract_names(tree)
        for classdef in iter_contract_classes(tree, foreign):
            for stmt in classdef.body:
                if not (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and _is_text_field(stmt.annotation)
                ):
                    continue
                name = stmt.target.id
                hit = next(
                    (
                        site
                        for site in evidence.get(name, ())
                        if classdef.name in lineages.get(site[0], frozenset({site[0]}))
                    ),
                    None,
                )
                if hit is None:
                    continue
                _, ev_path, kind, line = hit
                try:
                    ev_rel = str(ev_path.relative_to(root))
                except ValueError:
                    ev_rel = str(ev_path)
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id="P059",
                        node=stmt,
                        message=(
                            f"Contract field '{name}' on '{classdef.name}' carries JSON "
                            f"text: its value is {kind} as JSON at {ev_rel}:{line}. The "
                            "contract declares a string, so payload safety (P001), the "
                            "unmodeled-container check (P015) and the contract ledger "
                            "cannot see the real shape or its size. Declare the decoded "
                            "value as a typed field instead (a pydantic.BaseModel, or "
                            "list[Model] for a list of records); hand a source-sized "
                            "value over as a FileReference. Suppress with "
                            "'# conformance: ignore[P059] <reason>' only when a system "
                            "outside the app needs the value as JSON text."
                        ),
                        directives=file_directives.get(path, {}),
                    )
                )
    return findings

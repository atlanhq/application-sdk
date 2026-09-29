"""P053 LocalCredentialRouting — app routes credential channels itself.

Fires on the fingerprints a hand-rolled ``build_credential_ref`` leaves.  See
``rules/credential_seam.py`` for why these shapes (and not "re-implements the
router", which is not statically decidable) are what the rule keys off.

Routing
-------
Two shapes, grouped per function:

* **ref routing** — a call to ``CredentialRef.resolve(...)`` /
  ``CredentialRef.resolve_or_none(...)``, or a ``CredentialRef(...)``
  construction whose ``credential_guid=`` is the input's own GUID channel —
  ``<x>.credential_guid``, ``<x>["credential_guid"]``,
  ``<x>.get("credential_guid", ...)`` (optionally behind an ``or`` default), or
  a local name bound from one of those in the same function or at module level.
  A GUID from any other field (``CredentialRef(credential_guid=
  input.cloud_source)``) is a second, per-source credential the seam does not
  model, and stays silent.  The keyword is what separates routing from naming:
  a ``CredentialRef(name="x", credential_type="basic")`` points at one named
  secret and is not a decision about which input channel wins, so it stays
  silent — as does
  ``CredentialRef(agent_spec=...)``, which builds an agent ref from a spec the
  app already holds.
* **inline flattening** — a ``for`` loop or comprehension over a
  credentials-named iterable (its source text contains ``cred``) whose loop
  variable is read as ``item["key"]`` *and* ``item["value"]`` /
  ``item.get("value", ...)``: the app's own flattening of the v3
  ``[{key, value}]`` inline pairs.  The ``cred`` gate is what keeps ordinary
  key/value pair handling (tags, parameters, headers) silent; a credentials list
  renamed to something without ``cred`` in it is an accepted false-negative.
  This is the only fingerprint a dict-based router (``workflow_args.get(
  "credential_guid")`` then ``workflow_args.get("credentials", [])``) leaves.

One finding per function, anchored at its first site, naming every shape the
function has: a local router typically tries ``resolve``, falls back to a
GUID-built ref, then flattens inline pairs, and that is one function to migrate,
not three.  A nested ``def`` is its own function.  Sites at module or class-body
level have no function to group under and are reported individually.

``CredentialRef`` is recognised bare, through a ``from application_sdk...
import CredentialRef as <alias>`` rename, and module-qualified
(``ref.CredentialRef.resolve``).  There is no import gate beyond that: a
re-export through an app-local module still names the SDK class.

Local credential types
----------------------
A module-level alias named ``CredentialValue``, ``CredentialMap``,
``InlineCredentials`` or ``Bounded*Credential*`` (one leading underscore
allowed) whose value is a type union (``a | b``, ``Union[...]``,
``Optional[...]``) or a ``dict`` / ``Annotated`` / ``Mapping`` / ``list``
subscript.  Plain ``=``, annotated (``X: TypeAlias = ...``) and PEP 695
``type X = ...`` forms all count.  Re-binding the name to something imported
(``CredentialValue = sdk.CredentialValue``) is not a type shape and stays silent.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

RULE_ID = "P053"

_CREDENTIAL_REF = "CredentialRef"
_ROUTING_METHODS = frozenset({"resolve", "resolve_or_none"})
_ROUTING_KEYWORD = "credential_guid"
_INLINE_SHAPE = "inline [{key, value}] flattening"
_CREDENTIALS_ITERABLE_RE = re.compile(r"cred", re.IGNORECASE)

#: The type names the SDK's credential seam exports.  A module-level alias with
#: one of these names is the app's own copy of that type.
_SEAM_TYPE_NAMES = frozenset({"CredentialValue", "CredentialMap", "InlineCredentials"})
_BOUNDED_CREDENTIAL_RE = re.compile(r"^Bounded\w*Credential\w*$")

#: Subscripted generics a credential alias is built from.
_TYPE_SUBSCRIPTS = frozenset(
    {
        "Annotated",
        "Dict",
        "List",
        "Mapping",
        "MutableMapping",
        "Optional",
        "Union",
        "dict",
        "list",
    }
)

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _credential_ref_aliases(tree: ast.AST) -> frozenset[str]:
    """Local names bound to the SDK's ``CredentialRef`` in this module."""
    names = {_CREDENTIAL_REF}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("application_sdk"):
            continue
        for alias in node.names:
            if alias.name == _CREDENTIAL_REF and alias.asname:
                names.add(alias.asname)
    return frozenset(names)


def _is_credential_ref(node: ast.expr, aliases: frozenset[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    return isinstance(node, ast.Attribute) and node.attr == _CREDENTIAL_REF


def _is_own_guid_read(node: ast.expr) -> bool:
    """True if *node* reads the input's own ``credential_guid`` channel directly.

    ``<x>.credential_guid``, ``<x>["credential_guid"]`` and
    ``<x>.get("credential_guid", ...)``, optionally behind an ``or`` default
    (``input.credential_guid or ""``).  A GUID read from any other field — a
    second, per-source credential such as ``input.cloud_source`` — is not the
    input's routing channel and does not count.
    """
    if isinstance(node, ast.BoolOp):
        return any(_is_own_guid_read(value) for value in node.values)
    if isinstance(node, ast.Attribute):
        return node.attr == _ROUTING_KEYWORD
    if isinstance(node, ast.Subscript):
        return _is_str_constant(node.slice, _ROUTING_KEYWORD)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and _is_str_constant(node.args[0], _ROUTING_KEYWORD)
    )


def _routing_shape(
    call: ast.Call, aliases: frozenset[str], guid_names: frozenset[str]
) -> str | None:
    """The ref-routing shape *call* has, spelled for the finding, or ``None``.

    *guid_names* are the local names bound from an own-GUID read in scope, so
    ``guid = input.credential_guid; CredentialRef(credential_guid=guid)`` fires.
    """
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _ROUTING_METHODS
        and _is_credential_ref(func.value, aliases)
    ):
        return f"CredentialRef.{func.attr}(...)"
    if _is_credential_ref(func, aliases) and any(
        kw.arg == _ROUTING_KEYWORD
        and (
            _is_own_guid_read(kw.value)
            or (isinstance(kw.value, ast.Name) and kw.value.id in guid_names)
        )
        for kw in call.keywords
    ):
        return "CredentialRef(credential_guid=...)"
    return None


def _is_str_constant(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _reads_pair(nodes: list[ast.AST], var: str) -> bool:
    """True if *nodes* read ``var["key"]`` and ``var["value"]`` / ``var.get("value")``."""
    reads_key = reads_value = False
    for root in nodes:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == var
            ):
                reads_key |= _is_str_constant(node.slice, "key")
                reads_value |= _is_str_constant(node.slice, "value")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == var
                and node.args
            ):
                reads_key |= _is_str_constant(node.args[0], "key")
                reads_value |= _is_str_constant(node.args[0], "value")
    return reads_key and reads_value


def _is_credentials_iterable(node: ast.expr) -> bool:
    return bool(_CREDENTIALS_ITERABLE_RE.search(ast.unparse(node)))


def _parameter_names(node: _FunctionNode) -> set[str]:
    args = node.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    params += [a for a in (args.vararg, args.kwarg) if a is not None]
    return {a.arg for a in params}


@dataclass
class _Scope:
    """The routing sites seen in one function, in source order."""

    function: _FunctionNode | None
    sites: list[tuple[ast.AST, str]] = field(default_factory=list)
    guid_names: set[str] = field(default_factory=set)
    """Names bound in this function from an own-GUID read."""
    shadowed: set[str] = field(default_factory=set)
    """Parameters and every name this function assigns — they hide a module
    name of the same spelling, whatever the module bound it to."""

    def anchor(self) -> ast.AST:
        return min(
            (node for node, _ in self.sites),
            key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)),
        )

    def shapes(self) -> list[str]:
        return list(dict.fromkeys(shape for _, shape in self.sites))


class _RoutingVisitor(ast.NodeVisitor):
    """Group routing sites by enclosing function (each unscoped site alone)."""

    def __init__(self, aliases: frozenset[str]) -> None:
        self._aliases = aliases
        self._stack: list[_Scope] = []
        self.scopes: list[_Scope] = []
        self._module_guid_names: set[str] = set()

    def _guid_names(self) -> frozenset[str]:
        """Own-GUID names visible here: the current function's, then the module's
        names it does not shadow with a parameter or an assignment of its own."""
        if not self._stack:
            return frozenset(self._module_guid_names)
        scope = self._stack[-1]
        return frozenset(scope.guid_names | (self._module_guid_names - scope.shadowed))

    def _bind(self, target: ast.expr, value: ast.expr | None) -> None:
        if not isinstance(target, ast.Name) or value is None:
            return
        if self._stack:
            self._stack[-1].shadowed.add(target.id)
        names = self._stack[-1].guid_names if self._stack else self._module_guid_names
        if _is_own_guid_read(value) or (
            isinstance(value, ast.Name) and value.id in self._guid_names()
        ):
            names.add(target.id)
        else:
            names.discard(target.id)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        self._bind(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self._bind(node.target, node.value)

    def _record(self, node: ast.AST, shape: str) -> None:
        if self._stack:
            self._stack[-1].sites.append((node, shape))
        else:
            self.scopes.append(_Scope(function=None, sites=[(node, shape)]))

    def _visit_function(self, node: _FunctionNode) -> None:
        scope = _Scope(function=node, shadowed=_parameter_names(node))
        self._stack.append(scope)
        self.generic_visit(node)
        self._stack.pop()
        if scope.sites:
            self.scopes.append(scope)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        shape = _routing_shape(node, self._aliases, self._guid_names())
        if shape is not None:
            self._record(node, shape)
        self.generic_visit(node)

    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        if (
            isinstance(node.target, ast.Name)
            and _is_credentials_iterable(node.iter)
            and _reads_pair(list(node.body), node.target.id)
        ):
            self._record(node, _INLINE_SHAPE)
        self.generic_visit(node)

    visit_For = _visit_loop
    visit_AsyncFor = _visit_loop

    def _visit_comprehension(
        self, node: ast.DictComp | ast.ListComp | ast.SetComp | ast.GeneratorExp
    ) -> None:
        produced: list[ast.AST] = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        # Filters read the pair too: ``{i["key"]: v for i in creds if (v := i["value"])}``.
        filters: list[ast.AST] = [cond for gen in node.generators for cond in gen.ifs]
        for gen in node.generators:
            if (
                isinstance(gen.target, ast.Name)
                and _is_credentials_iterable(gen.iter)
                and _reads_pair(produced + filters, gen.target.id)
            ):
                self._record(node, _INLINE_SHAPE)
                break
        self.generic_visit(node)

    visit_DictComp = _visit_comprehension
    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension


def _routing_message(shapes: list[str], function: str | None) -> str:
    where = f"`{function}` routes" if function else "This code routes"
    listed = ", ".join(shapes)
    return (
        f"{where} a workflow input's credential channels itself ({listed}) instead "
        "of through the SDK's credential seam. Use "
        "`ref, inline = route_credentials(input)` from application_sdk.credentials "
        "— it prefers a pre-built CredentialRef field (run_credential_field "
        "ClassVar when there are several), routes "
        "credential_guid / agent_json through CredentialRef.resolve, and normalises "
        "inline credentials into one CredentialMap (normalize_inline_credentials "
        "does that half alone for a dict-shaped payload) — and read the pair on the "
        "task side with self.context.resolve_credential_raw_or_inline(ref, inline). "
        "Local copies drift on agent routing, strictness and the inline shape, so "
        "the same input resolves differently per app. If this resolves something "
        "other than the entry-point input, justify it with "
        f"'# conformance: ignore[{RULE_ID}] <reason>'."
    )


def _is_type_shape(value: ast.expr) -> bool:
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return True
    if isinstance(value, ast.Subscript):
        head = value.value
        name = (
            head.id
            if isinstance(head, ast.Name)
            else head.attr
            if isinstance(head, ast.Attribute)
            else None
        )
        return name in _TYPE_SUBSCRIPTS
    return False


def _is_seam_type_name(name: str) -> bool:
    bare = name[1:] if name.startswith("_") else name
    return bare in _SEAM_TYPE_NAMES or bool(_BOUNDED_CREDENTIAL_RE.match(bare))


def _module_type_aliases(tree: ast.Module) -> list[tuple[ast.stmt, str]]:
    """Module-level ``(statement, name)`` pairs declaring a local credential type."""
    type_alias_node = getattr(ast, "TypeAlias", None)  # PEP 695, Python 3.12+
    hits: list[tuple[ast.stmt, str]] = []
    for stmt in tree.body:
        name: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name, value = stmt.target.id, stmt.value
        elif type_alias_node is not None and isinstance(stmt, type_alias_node):
            alias_name = getattr(stmt, "name", None)
            if isinstance(alias_name, ast.Name):
                name, value = alias_name.id, getattr(stmt, "value", None)
        if name is None or value is None:
            continue
        if _is_seam_type_name(name) and _is_type_shape(value):
            hits.append((stmt, name))
    return hits


def _type_message(name: str) -> str:
    return (
        f"`{name}` is a local copy of a credential type the SDK's credential seam "
        "exports. Import CredentialValue / CredentialMap / InlineCredentials from "
        "application_sdk.credentials instead — the SDK's CredentialMap is the "
        "bounded, flat, dotted-key shape route_credentials produces, so a contract "
        "field typed with it accepts exactly what the router hands it, where a "
        "local alias can disagree on the scalar set or the bound."
    )


def check_p053(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P053 for local credential routing and local credential-type aliases."""
    findings: list[Finding] = []
    visitor = _RoutingVisitor(_credential_ref_aliases(tree))
    visitor.visit(tree)
    for scope in visitor.scopes:
        findings.append(
            make_finding(
                filename=filename,
                rule_id=RULE_ID,
                node=scope.anchor(),
                message=_routing_message(
                    scope.shapes(), scope.function.name if scope.function else None
                ),
                directives=directives,
            )
        )
    if isinstance(tree, ast.Module):
        for stmt, name in _module_type_aliases(tree):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=stmt,
                    message=_type_message(name),
                    directives=directives,
                )
            )
    findings.sort(key=lambda f: (f.line, f.column))
    return findings

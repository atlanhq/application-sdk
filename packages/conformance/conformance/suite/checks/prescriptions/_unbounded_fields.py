"""P001 UnboundedContractFields — the payload-safety opt-out, in both directions.

An ``Input``/``Output`` contract subclass declared with the
``allow_unbounded_fields=True`` class keyword, or with a truthy
``_allow_unbounded_fields`` class attribute (the runtime honours both), opts
out of payload-safety enforcement. The fix is to type every field
payload-safely and drop the opt-out; an opt-out that genuinely cannot be
removed must carry an inline, justified suppression at the declaration site.

Each finding names, from the fields the class declares, those payload safety
refuses (they block removing the opt-out), those it accepts that still bound
nothing, and those whose type this file cannot resolve, so the fix starts from
the actual fields. Same-file aliases and quoted annotations are resolved the way
``get_type_hints`` resolves them; an imported type is reported as unknown, never
as safe, and a known SDK name imported from anywhere but its trusted origin (or
renamed onto it) is treated as that unknown import. Many opt-outs guard nothing: every declared field is already safe, and
the finding says so, because removing the opt-out is then the whole fix.

Either form is inherited: ``__init_subclass__`` stores the keyword as the class
attribute, so a subclass that does not repeat it stays opted out, and a falsy
keyword on the subclass does not opt it back in. Nor does a falsy attribute on a
contract unless it is annotated ``ClassVar``: pydantic keeps any other underscore
assignment as a private attribute, which is truthy. The check follows this through
every class in the same file, and reports a subclass of an opted-out contract only
for the fields it adds; an opt-out inherited from another module is not seen.

The rule also catches the INVERSE, which is how a well-meaning remediation
breaks an app: drop the opt-out from a class that still has an ``Any``-typed
field and the finding goes away, but ``Input.__init_subclass__`` raises
``PayloadSafetyError`` at class-definition time and the app no longer imports.
``Any`` is refused unconditionally — wrapping it in ``MaxItems`` does not help —
so a class in that state is dead code that no detector used to notice, and
``py_compile`` cannot see it either.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: Bases that put a class under payload-safety enforcement. Matched by NAME, and
#: only when named directly: this check stays inside one file, and a
#: conservative miss is much cheaper than telling an app its contract is broken
#: when it is not.
_CONTRACT_BASES = frozenset({"Input", "Output"})


def _simple_name(node: ast.expr | None) -> str | None:
    """Bare or dotted terminal name (``Any``, ``typing.Any`` → ``Any``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _annotated_arg(node: ast.expr | None) -> ast.expr | None:
    """Inner type of ``Annotated[T, ...]``, or ``None`` if *node* is not that."""
    if not isinstance(node, ast.Subscript) or _simple_name(node.value) != "Annotated":
        return None
    sl = node.slice
    if isinstance(sl, ast.Tuple) and sl.elts:
        return sl.elts[0]
    return sl


def _unwrap_annotated(node: ast.expr | None) -> ast.expr | None:
    """Strip leading ``Annotated[...]`` wrappers, matching runtime ``get_origin``."""
    while node is not None:
        inner = _annotated_arg(node)
        if inner is None:
            return node
        node = inner
    return node


def _is_classvar_annotation(annotation: ast.expr | None) -> bool:
    """True if the annotation is ``ClassVar`` / ``ClassVar[...]`` (any form).

    Runtime ``validate_payload_safety`` skips ``ClassVar`` regardless of the
    field name — unwrap ``Annotated`` first so ``Annotated[ClassVar[Any], ...]``
    is not a false positive either.
    """
    node = _unwrap_annotated(annotation)
    if node is None:
        return False
    if isinstance(node, ast.Subscript):
        return _simple_name(node.value) == "ClassVar"
    return _simple_name(node) == "ClassVar"


#: Collections payload safety refuses without a ``MaxItems`` bound. The runtime refuses every
#: parameterised iterable class (by ``get_origin``), so retyping a list as a set or a Sequence
#: does not get past it; these are the names this file can recognise as one.
_BOUNDED_COLLECTIONS = frozenset(
    {
        "dict",
        "list",
        "set",
        "frozenset",
        "tuple",
        "deque",
        "defaultdict",
        "OrderedDict",
        "Counter",
        "Dict",
        "List",
        "Set",
        "FrozenSet",
        "Tuple",
        "Deque",
        "DefaultDict",
        "AbstractSet",
        "MutableSet",
        "Sequence",
        "MutableSequence",
        "Mapping",
        "MutableMapping",
        "Collection",
        "Iterable",
    }
)
#: A ``tuple[A, B]`` of fixed arity is bounded by its shape; only ``tuple[T, ...]`` needs MaxItems.
_TUPLES = frozenset({"tuple", "Tuple"})
_BYTES = frozenset({"bytes", "bytearray"})
#: Accepted by the runtime check (it skips an unparameterised collection) while bounding nothing.
_BARE_UNBOUNDED = _BOUNDED_COLLECTIONS
#: Types the runtime check accepts that still let any value through: retyping a field to one of
#: these clears the class-definition error without bounding the payload.
_OPEN_ENDED = frozenset({"object", "JsonValue"})
#: Field types known to be payload-safe without seeing their definition: scalars and the SDK's
#: payload-safe contract types.
_KNOWN_SAFE = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "complex",
        "None",
        "NoneType",
        "datetime",
        "date",
        "time",
        "timedelta",
        "Decimal",
        "UUID",
        "SecretStr",
        "FilterMap",
        "TreeSelection",
        "FileReference",
        "Lazy",
        "ConnectionRef",
        "ConnectionAttributes",
        "CredentialRef",
        "AgentCredentialSpec",
        "GitReference",
        "BaseMetadataConfig",
    }
)
_OPTOUT_ATTR = "_allow_unbounded_fields"
#: How many fields a finding names per group before summarising the rest.
_LISTED_FIELDS = 6
#: Verdict kinds, worst first: a field reports its worst.
_REFUSED, _OPEN, _UNKNOWN = "refused", "open", "unknown"


def _has_max_items(node: ast.expr) -> bool:
    """True if *node* is ``Annotated[T, ..., MaxItems(n), ...]``."""
    if not isinstance(node, ast.Subscript) or _simple_name(node.value) != "Annotated":
        return False
    sl = node.slice
    metadata = sl.elts[1:] if isinstance(sl, ast.Tuple) else []
    return any(
        isinstance(m, ast.Call) and _simple_name(m.func) == "MaxItems" for m in metadata
    )


def _args(node: ast.Subscript) -> list[ast.expr]:
    return list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]


def _fixed_tuple(node: ast.Subscript) -> bool:
    """True if *node* is a ``tuple[A, B]`` of fixed arity (no ``...`` argument)."""
    return _simple_name(node.value) in _TUPLES and not any(
        isinstance(a, ast.Constant) and a.value is Ellipsis for a in _args(node)
    )


def _is_alias_value(node: ast.expr) -> bool:
    """True if a module-level assignment's value reads as a type, so the name is a type alias."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.BitOr)
    return isinstance(node, (ast.Name, ast.Attribute, ast.Subscript))


#: Modules a known-safe name may be imported from and still mean the known type.
_TRUSTED_ORIGINS = (
    "application_sdk",
    "builtins",
    "datetime",
    "decimal",
    "uuid",
    "typing",
    "typing_extensions",
    "pydantic",
)


def _untrusted_import_bindings(node: ast.Module) -> frozenset[str]:
    """Names bound by an import that cannot be taken to mean the known-safe type of that name.

    ``from x import Thing as TreeSelection``, ``from .types import TreeSelection`` and
    ``from some_lib import TreeSelection`` all bind ``TreeSelection`` to a type this file
    cannot see; only an unrenamed import from a trusted origin keeps the known meaning.
    """
    out: set[str] = set()
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            trusted = stmt.level == 0 and any(
                module == m or module.startswith(m + ".") for m in _TRUSTED_ORIGINS
            )
            for a in stmt.names:
                if not trusted or (a.asname is not None and a.asname != a.name):
                    out.add(a.asname or a.name)
    return frozenset(out)


class _Types:
    """Judges field annotations as ``validate_payload_safety`` would, within one file.

    Same-file type aliases and quoted forward references are resolved, as ``get_type_hints``
    resolves them at runtime. A name this file neither defines nor knows is reported as
    unknown, never as safe: telling an owner a field is safe when its type hides an ``Any``
    would have them remove the opt-out and break the import.
    """

    def __init__(
        self,
        aliases: dict[str, ast.expr],
        local_classes: set[str],
        local_names: frozenset[str] = frozenset(),
    ) -> None:
        self._aliases = aliases
        self._local = local_classes
        self._local_names = local_names

    def judge(
        self,
        node: ast.expr | None,
        *,
        bounded: bool = False,
        seen: frozenset[str] = frozenset(),
    ) -> list[tuple[str, str]]:
        """Every ``(kind, why)`` problem in *node*."""
        if node is None:
            return []
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                return []
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return [(_UNKNOWN, f"the forward reference {node.value!r}")]
            return self.judge(parsed, bounded=bounded, seen=seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.judge(node.left, seen=seen) + self.judge(node.right, seen=seen)
        if isinstance(node, ast.Subscript):
            name = _simple_name(node.value)
            if name == "Annotated":
                return self.judge(
                    _annotated_arg(node), bounded=_has_max_items(node), seen=seen
                )
            if name in ("ClassVar", "Literal"):
                return []
            out: list[tuple[str, str]] = []
            if name in _BOUNDED_COLLECTIONS and not bounded and not _fixed_tuple(node):
                out.append((_REFUSED, f"`{name}[...]` without MaxItems"))
            elif name not in ("Optional", "Union") and name not in _BOUNDED_COLLECTIONS:
                out += self._name(node.value, bounded=bounded, seen=seen)
            for arg in _args(node):
                out += self.judge(arg, seen=seen)
            return out
        return self._name(node, bounded=bounded, seen=seen)

    def _name(
        self, node: ast.expr, *, bounded: bool, seen: frozenset[str]
    ) -> list[tuple[str, str]]:
        name = _simple_name(node)
        if name is None:
            return [(_UNKNOWN, f"`{ast.unparse(node)}`")]
        if name == "Any":
            return [(_REFUSED, "`Any`")]
        if name in _BYTES:
            return [(_REFUSED, f"`{name}` (pass it by FileReference)")]
        if name in _OPEN_ENDED:
            return [
                (
                    _OPEN,
                    f"`{name}` (open-ended: the runtime check accepts it, but it bounds nothing)",
                )
            ]
        if name in _BARE_UNBOUNDED:
            return [
                (
                    _OPEN,
                    f"bare `{name}` (the runtime check accepts it, but it is unbounded)",
                )
            ]
        if isinstance(node, ast.Name) and name in self._aliases and name not in seen:
            return self.judge(self._aliases[name], bounded=bounded, seen=seen | {name})
        if name in self._local or (
            name in _KNOWN_SAFE and name not in self._local_names
        ):
            return []
        return [(_UNKNOWN, f"`{name}`, a type this file does not define")]

    def fields(self, node: ast.ClassDef) -> dict[str, list[tuple[str, str]]]:
        """The class's own payload fields grouped by their worst verdict: ``{kind: [(field, why)]}``."""
        groups: dict[str, list[tuple[str, str]]] = {
            _REFUSED: [],
            _OPEN: [],
            _UNKNOWN: [],
        }
        for stmt in node.body:
            if not (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ):
                continue
            if stmt.target.id.startswith("_") or _is_classvar_annotation(
                stmt.annotation
            ):
                continue
            # `Any` first: it is the blocker no MaxItems can fix, so it names the field's real problem.
            problems = sorted(
                self.judge(stmt.annotation), key=lambda p: p[1] != "`Any`"
            )
            for kind in (_REFUSED, _OPEN, _UNKNOWN):
                why = next((w for k, w in problems if k == kind), None)
                if why:
                    groups[kind].append((stmt.target.id, why))
                    break
        return groups


def _listed(items: list[tuple[str, str]]) -> str:
    text = "; ".join(f"{field}: {why}" for field, why in items[:_LISTED_FIELDS])
    if len(items) > _LISTED_FIELDS:
        text += f"; and {len(items) - _LISTED_FIELDS} more"
    return text


def _optout_message(
    name: str,
    via: str,
    groups: dict[str, list[tuple[str, str]]],
    inherited_from: str | None = None,
) -> str:
    refused, open_ended, unknown = groups[_REFUSED], groups[_OPEN], groups[_UNKNOWN]
    if inherited_from:
        head = (
            f"Contract '{name}' inherits the payload-safety opt-out from {inherited_from} via {via}, "
            "so the fields it adds are not checked either"
        )
    else:
        head = f"Contract '{name}' opts out of payload-safety enforcement via {via}"
    if not (refused or open_ended or unknown):
        return (
            f"{head}, but every field it declares is already payload-safe. Fix: remove the opt-out "
            "(the class keyword, or the _allow_unbounded_fields attribute). Then import the module: a "
            "field inherited from a base class or mixin that payload safety refuses raises "
            "PayloadSafetyError there, and is fixed in that class."
        )
    parts = [head + "."]
    if refused:
        parts.append(
            f"Fields payload safety refuses, which block removing the opt-out: {_listed(refused)}."
        )
    if open_ended:
        parts.append(
            f"Fields it accepts that still bound nothing: {_listed(open_ended)}."
        )
    if unknown:
        parts.append(
            "Fields typed with names this file cannot resolve; check each before removing the opt-out: "
            f"{_listed(unknown)}."
        )
    if inherited_from:
        parts.append(
            f"They must be payload-safe before {inherited_from} drops its opt-out (its own P001 "
            "finding), or this class raises PayloadSafetyError at import."
        )
    if refused or open_ended:
        parts.append(
            ("Fix each" if inherited_from else "Fix each, then remove the opt-out")
            + ": drop an override of a field the SDK base already types "
            "(connection: ConnectionRef; include/exclude filters: FilterMap | str, or treeSelection = true "
            "on a tree widget read below its second level); model passthrough data "
            "(metadata: a BaseMetadataConfig subclass; credentials by reference: credential_guid or "
            "CredentialRef, never inline); bound a collection of safe values (dict, list, set, "
            "tuple[T, ...], Sequence) with "
            "Annotated[..., MaxItems(n)]; pass data that grows with the source system by FileReference. "
            "MaxItems never makes Any safe."
        )
    elif not inherited_from:
        parts.append(
            "If those types are payload-safe, remove the opt-out, then import the module to confirm."
        )
    parts.append(
        (
            "If they cannot be, add '# conformance: ignore[P001] <reason>' here too, naming the "
            "alternatives tried."
        )
        if inherited_from
        else (
            "Keep the opt-out only as a last resort, with '# conformance: ignore[P001] <reason>' "
            "naming the alternatives tried."
        )
    )
    return " ".join(parts)


def _attribute_last(node: ast.ClassDef) -> tuple[ast.stmt, ast.expr] | None:
    """The last ``_allow_unbounded_fields`` assignment in the class body, the one the runtime sees."""
    last: tuple[ast.stmt, ast.expr] | None = None
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == _OPTOUT_ATTR for t in targets):
            last = (stmt, value)
    return last


def _is_falsy_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def _becomes_private_attr(stmt: ast.stmt, is_contract: bool) -> bool:
    """True if Pydantic turns this underscore assignment into a private attribute.

    On a contract, a non-``ClassVar`` underscore assignment becomes a ``ModelPrivateAttr``, and
    reading it from the class returns that descriptor, which is truthy whatever literal it holds.
    """
    return is_contract and not (
        isinstance(stmt, ast.AnnAssign) and _is_classvar_annotation(stmt.annotation)
    )


def _attribute_optout(node: ast.ClassDef, is_contract: bool) -> ast.stmt | None:
    """The statement leaving ``_allow_unbounded_fields`` truthy on the class, if any.

    The last assignment in the class body is the one the runtime sees, so an earlier falsy one
    does not hide a later truthy one. On a contract a falsy literal still counts unless it is a
    ``ClassVar`` (see ``_becomes_private_attr``).
    """
    last = _attribute_last(node)
    if last is None:
        return None
    stmt, value = last
    if _is_falsy_literal(value) and not _becomes_private_attr(stmt, is_contract):
        return None
    return stmt


def _attribute_turns_off(node: ast.ClassDef, is_contract: bool) -> bool:
    """True if the class's last ``_allow_unbounded_fields`` assignment really leaves it falsy."""
    return (
        _attribute_last(node) is not None
        and _attribute_optout(node, is_contract) is None
    )


def _module_scope_classes(node: ast.Module) -> list[ast.ClassDef]:
    """Classes whose name binds at module level (not inside a function or another class)."""
    out: list[ast.ClassDef] = []
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, ast.ClassDef):
            out.append(current)
        elif not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            stack.extend(
                child
                for child in ast.iter_child_nodes(current)
                if isinstance(child, ast.stmt)
            )
    return out


def _keyword_optout(node: ast.ClassDef) -> bool:
    """True if the class passes a truthy ``allow_unbounded_fields`` keyword.

    ``Input``/``Output.__init_subclass__`` only ever sets the attribute to True, so a falsy
    keyword is not a decision: the class keeps whatever its bases set.
    """
    return any(
        kw.arg == "allow_unbounded_fields" and not _is_falsy_literal(kw.value)
        for kw in node.keywords
    )


def _base_names(node: ast.ClassDef) -> list[str]:
    return [n for b in node.bases if (n := _simple_name(b))]


def _mentions_any(node: ast.expr | None) -> bool:
    """True if ``Any`` appears anywhere in an annotation.

    Covers the bare name, dotted ``typing.Any``, and every nesting the fleet
    actually writes: ``dict[str, Any]``, ``list[dict[str, Any]]``,
    ``Annotated[dict[str, Any], MaxItems(50)]``, and unions of those.
    """
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "Any":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "Any":
            return True
    return False


def _any_typed_fields(node: ast.ClassDef) -> list[str]:
    """Names of annotated class fields whose type mentions ``Any``.

    Mirrors ``validate_payload_safety``: private names and ``ClassVar``
    annotations are not payload fields, so they must not trip the inverse.
    """
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and not stmt.target.id.startswith("_")
        and not _is_classvar_annotation(stmt.annotation)
        and _mentions_any(stmt.annotation)
    ]


def _is_contract_subclass(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in _CONTRACT_BASES:
            return True
    return False


class UnboundedContractFieldsChecker(ast.NodeVisitor):
    """Walk a module AST and emit P001 findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._findings: list[Finding] = []
        self._types = _Types({}, set())
        #: Classes that set a truthy _allow_unbounded_fields themselves.
        self._attr_optout: dict[ast.ClassDef, ast.stmt] = {}
        #: Classes that set the opt-out with the class keyword.
        self._keyword_optout: set[ast.ClassDef] = set()
        #: Classes that reach Input/Output through bases defined in this file.
        self._contracts: set[ast.ClassDef] = set()
        #: Classes that inherit the opt-out from a class in this file: class -> where it is set.
        self._inherited: dict[ast.ClassDef, ast.ClassDef] = {}
        #: Attribute-setting classes whose opt-out is reported on the contracts that inherit it.
        self._reported_on_subclass: set[ast.ClassDef] = set()

    def visit_Module(self, node: ast.Module) -> None:
        """Read what the class checks need from the whole file first: type aliases, the classes it
        defines, and which classes carry the attribute opt-out (theirs or inherited)."""
        aliases: dict[str, ast.expr] = {}
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and _is_alias_value(stmt.value)
            ):
                aliases[stmt.targets[0].id] = stmt.value
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
                and _simple_name(stmt.annotation) == "TypeAlias"
            ):
                aliases[stmt.target.id] = stmt.value
            elif type(stmt).__name__ == "TypeAlias":  # `type X = ...` (Python 3.12+)
                aliases[stmt.name.id] = stmt.value  # type: ignore[attr-defined]
        all_classes = [c for c in ast.walk(node) if isinstance(c, ast.ClassDef)]
        local_names = frozenset(
            t.id
            for stmt in node.body
            if isinstance(stmt, (ast.Assign, ast.AnnAssign))
            for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
            if isinstance(t, ast.Name)
        ) | _untrusted_import_bindings(node)
        self._types = _Types(aliases, {c.name for c in all_classes}, local_names)
        self._keyword_optout = {c for c in all_classes if _keyword_optout(c)}

        # One pass in source order: a base is defined before the class that names it, so each
        # base is already resolved, and a later redefinition of a name does not reach back.
        # ``source`` maps a class to the class whose finding covers its opt-out: the class that
        # sets it, or for a non-contract mixin, the first contract that inherits it, so the
        # contracts below that one are reported only for what they add.
        module_scope = set(_module_scope_classes(node))
        bound: dict[str, ast.ClassDef] = {}
        off = object()
        source: dict[ast.ClassDef, object] = {}
        for c in sorted(all_classes, key=lambda c: (c.lineno, c.col_offset)):
            bases = [bound[b] for b in _base_names(c) if b in bound]
            if _is_contract_subclass(c) or any(b in self._contracts for b in bases):
                self._contracts.add(c)
            found = next((source[b] for b in bases if b in source), None)
            stmt = _attribute_optout(c, c in self._contracts)
            last = _attribute_last(c)
            if found is not None and last is not None and _is_falsy_literal(last[1]):
                # Pydantic takes a private attribute out of the class namespace, so a lookup
                # reaches the base that sets the attribute before the truthy descriptor.
                stmt = None
            if stmt is not None:
                self._attr_optout[c] = stmt
            if c in self._keyword_optout or c in self._attr_optout:
                source[c] = c
            elif _attribute_turns_off(c, c in self._contracts):
                source[c] = off
            elif found is not None:
                source[c] = found
                if found is not off:
                    self._inherited[c] = found  # type: ignore[assignment]
                    if c in self._contracts and found not in self._contracts:
                        source[c] = c
            if c in module_scope:
                bound[c.name] = c
        self._reported_on_subclass = {
            s for c, s in self._inherited.items() if c in self._contracts
        } - self._contracts
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        opted_out = False
        if node in self._keyword_optout:
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=_optout_message(
                        node.name, "allow_unbounded_fields", self._types.fields(node)
                    ),
                    directives=self._directives,
                )
            )
            opted_out = True
        via = None
        inherited_from = None
        if not opted_out and node in self._attr_optout:
            if node not in self._reported_on_subclass:
                stmt = self._attr_optout[node]
                via = f"the {_OPTOUT_ATTR} class attribute (line {stmt.lineno})"
                last = _attribute_last(node)
                if last is not None and _is_falsy_literal(last[1]):
                    via += (
                        ", which Pydantic stores as a private attribute that is truthy whatever "
                        "it holds (annotate it ClassVar[bool] to keep the literal)"
                    )
            opted_out = (
                True  # a mixin's opt-out is reported on the contracts that inherit it
            )
        elif not opted_out and node in self._inherited and node in self._contracts:
            source = self._inherited[node]
            opted_out = True
            fields = self._types.fields(node)
            if source not in self._contracts:
                via = f"the {_OPTOUT_ATTR} attribute inherited from {source.name}"
            elif any(fields.values()):
                # A contract source carries its own finding, which also covers a subclass
                # that adds only safe fields; report the subclass only for what it adds.
                how = (
                    "the allow_unbounded_fields=True keyword"
                    if source in self._keyword_optout
                    else f"the {_OPTOUT_ATTR} attribute"
                )
                via = how
                inherited_from = source.name
        if via:
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=_optout_message(
                        node.name, via, self._types.fields(node), inherited_from
                    ),
                    directives=self._directives,
                )
            )
        if not opted_out:
            self._check_missing_optout(node)
        self.generic_visit(node)

    def _check_missing_optout(self, node: ast.ClassDef) -> None:
        """A contract with an ``Any`` field and no opt-out cannot be imported."""
        if node not in self._contracts:
            return
        fields = _any_typed_fields(node)
        if not fields:
            return
        self._findings.append(
            make_finding(
                filename=self._filename,
                rule_id="P001",
                node=node,
                message=(
                    f"Contract '{node.name}' declares Any-typed field(s) "
                    f"({', '.join(fields)}) but does NOT set "
                    "allow_unbounded_fields — payload-safety validation refuses Any "
                    "unconditionally, so this class raises PayloadSafetyError at "
                    "import and the app will not start. MaxItems does not help: bound "
                    "the value type instead (a concrete type, the SDK's FilterMap "
                    "for flat filter maps, or TreeSelection for a tree-widget "
                    "selection; not a str the dict is encoded into), or keep "
                    "allow_unbounded_fields with a justified "
                    "'# conformance: ignore[P001] <reason>' directive."
                ),
                directives=self._directives,
            )
        )

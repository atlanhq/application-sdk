"""P001 UnboundedContractFields — the payload-safety opt-out, in both directions.

An ``Input``/``Output`` contract subclass declared with the
``allow_unbounded_fields=True`` class keyword, or with a truthy
``_allow_unbounded_fields`` class attribute (the runtime honours both), opts
out of payload-safety enforcement. The fix is to type every field
payload-safely and drop the opt-out; an opt-out that genuinely cannot be
removed must carry an inline, justified suppression at the declaration site.

Each finding names the fields this class declares that payload safety would
refuse, and why, so the fix starts from the actual fields. Many opt-outs guard
nothing: every declared field is already safe, and the finding says so, because
removing the keyword is then the whole fix.

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


#: Collections payload safety requires a ``MaxItems`` bound on.
_COLLECTIONS = frozenset(
    {
        "dict",
        "list",
        "set",
        "frozenset",
        "tuple",
        "Dict",
        "List",
        "Set",
        "FrozenSet",
        "Tuple",
        "Mapping",
        "Sequence",
    }
)
_BYTES = frozenset({"bytes", "bytearray"})
_BARE_UNBOUNDED = frozenset({"dict", "list", "set", "Dict", "List", "Set"})
#: Types the runtime check accepts that still let any value through: retyping a field to one of
#: these clears the class-definition error without bounding the payload.
_OPEN_ENDED = frozenset({"object", "JsonValue"})
_OPTOUT_ATTR = "_allow_unbounded_fields"
#: How many unsafe fields a finding names before summarising the rest.
_LISTED_FIELDS = 6


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


def _why_unsafe(node: ast.expr | None, *, bounded: bool = False) -> str | None:
    """Why payload safety refuses this annotation, or why it is unbounded where the runtime
    check does not look (a bare ``dict``); None when it is safe or cannot be judged here.

    Mirrors ``validate_payload_safety``: ``Any`` and ``bytes`` are refused anywhere, and every
    collection, outer or nested, needs ``Annotated[..., MaxItems(n)]``. A name this file cannot
    resolve (``FilterMap``, a model class) is taken as safe: a conservative miss is cheaper
    than a wrong instruction.
    """
    if node is None or isinstance(node, ast.Constant):
        return None
    if _mentions_any(node):
        return "`Any`"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _why_unsafe(node.left) or _why_unsafe(node.right)
    if isinstance(node, ast.Subscript):
        name = _simple_name(node.value)
        if name == "Annotated":
            return _why_unsafe(_annotated_arg(node), bounded=_has_max_items(node))
        if name in ("Optional", "Union"):
            return next((w for a in _args(node) if (w := _why_unsafe(a))), None)
        if name in ("ClassVar", "Literal"):
            return None
        if name in _COLLECTIONS:
            if not bounded:
                return f"`{name}[...]` without MaxItems"
            return next((w for a in _args(node) if (w := _why_unsafe(a))), None)
        return next((w for a in _args(node) if (w := _why_unsafe(a))), None)
    name = _simple_name(node)
    if name in _BYTES:
        return f"`{name}` (pass it by FileReference)"
    if name in _OPEN_ENDED:
        return f"`{name}` (open-ended: the runtime check accepts it, but it bounds nothing)"
    if name in _BARE_UNBOUNDED:
        return f"bare `{name}` (unbounded, and the runtime check does not see it)"
    return None


def _unsafe_fields(node: ast.ClassDef) -> list[tuple[str, str]]:
    """``(field, why)`` for each payload field this class declares that payload safety would refuse."""
    out = []
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        if stmt.target.id.startswith("_") or _is_classvar_annotation(stmt.annotation):
            continue
        if why := _why_unsafe(stmt.annotation):
            out.append((stmt.target.id, why))
    return out


def _optout_message(name: str, via: str, unsafe: list[tuple[str, str]]) -> str:
    head = f"Contract '{name}' opts out of payload-safety enforcement via {via}"
    if not unsafe:
        return (
            f"{head}, but every field it declares is already payload-safe. Fix: remove the opt-out. "
            "Then import the module: a field inherited from a base class or mixin that payload safety "
            "refuses raises PayloadSafetyError there, and is fixed in that class."
        )
    listed = "; ".join(f"{field}: {why}" for field, why in unsafe[:_LISTED_FIELDS])
    if len(unsafe) > _LISTED_FIELDS:
        listed += f"; and {len(unsafe) - _LISTED_FIELDS} more"
    return (
        f"{head}. Fields payload safety would refuse: {listed}. Fix each, then remove the opt-out: "
        "drop an override of a field the SDK base already types (connection: ConnectionRef; "
        "include/exclude filters: FilterMap | str); model passthrough data (metadata: a BaseMetadataConfig "
        "subclass; credentials by reference: credential_guid or CredentialRef, never inline); bound a "
        "collection of safe values with Annotated[..., MaxItems(n)]; pass data that grows with the source "
        "system by FileReference. MaxItems never makes Any safe. Keep the opt-out only as a last resort, "
        "with '# conformance: ignore[P001] <reason>' naming the alternatives tried."
    )


def _attribute_optout(node: ast.ClassDef) -> ast.stmt | None:
    """The statement setting a truthy ``_allow_unbounded_fields`` class attribute, if any."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == _OPTOUT_ATTR for t in targets):
            continue
        if isinstance(value, ast.Constant) and not value.value:
            return None
        return stmt
    return None


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

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Do not couple the two checks through for/else: a falsy literal
        # (False/None/0/"") is a genuine opt-back-in at runtime
        # (``if allow_unbounded_fields:`` is false, so validation still
        # runs and raises PayloadSafetyError). The inverse must still fire.
        opted_out = False
        for kw in node.keywords:
            if kw.arg != "allow_unbounded_fields":
                continue
            # The opt-out is active for ANY truthy value: Input/Output's
            # __init_subclass__ does ``if allow_unbounded_fields:``.  So
            # ``=True``, ``=1`` and dynamic values (``=FLAG``, ``=(expr)``) all
            # opt out.  Only an explicit literal-falsy value (False/None/0/"")
            # is a genuine opt-back-in and must NOT be flagged as an opt-out.
            if isinstance(kw.value, ast.Constant) and not kw.value.value:
                break
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=_optout_message(
                        node.name, "allow_unbounded_fields", _unsafe_fields(node)
                    ),
                    directives=self._directives,
                )
            )
            opted_out = True
            break
        if not opted_out and (stmt := _attribute_optout(node)) is not None:
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=_optout_message(
                        node.name,
                        f"the {_OPTOUT_ATTR} class attribute (line {stmt.lineno})",
                        _unsafe_fields(node),
                    ),
                    directives=self._directives,
                )
            )
            opted_out = True
        if not opted_out:
            self._check_missing_optout(node)
        self.generic_visit(node)

    def _check_missing_optout(self, node: ast.ClassDef) -> None:
        """A contract with an ``Any`` field and no opt-out cannot be imported."""
        if not _is_contract_subclass(node):
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
                    "the value type instead (a concrete type, or the SDK's FilterMap "
                    "for filter maps), or keep allow_unbounded_fields with a justified "
                    "'# conformance: ignore[P001] <reason>' directive."
                ),
                directives=self._directives,
            )
        )

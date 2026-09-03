"""P001 UnboundedContractFields — the payload-safety opt-out, in both directions.

An ``Input``/``Output`` contract subclass declared with the
``allow_unbounded_fields=True`` class keyword opts out of payload-safety
enforcement; the opt-out must be an inline, justified suppression at the
declaration site.

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
                    message=(
                        f"Contract '{node.name}' opts out of payload-safety "
                        "enforcement via allow_unbounded_fields — arbitrary untyped "
                        "fields may cross task boundaries. This must be exceptional: "
                        "justify it with an inline '# conformance: ignore[P001] "
                        "<reason>' directive at the declaration site (and prefer a "
                        "non-dynamic value so the opt-out is statically auditable)."
                    ),
                    directives=self._directives,
                )
            )
            opted_out = True
            break
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

"""Shared AST helpers for contract-field inspection (P011 RawBytesInContract, P012 FilePathStringInContract).

A *contract class* is a class whose base list names ``Input`` or ``Output``
imported from ``application_sdk.*`` — the SDK's typed task-boundary contracts.
P011/P012 inspect the annotated fields of such classes, so the
type-annotation recognisers and field-doc extraction live here, away from any
single rule module.

Import provenance
-----------------
``iter_contract_classes`` gates on import provenance: a class is only
considered a contract when its ``Input``/``Output`` base is *not* explicitly
imported from a non-SDK module (e.g. pydantic_ai, strawberry, graphene).  If a
file imports ``Output`` from a third-party library, classes extending that
``Output`` are silently skipped.  Classes with no import for the base name are
assumed SDK (common in real apps that have the import elsewhere in the tree).
"""

from __future__ import annotations

import ast
from typing import Iterator

from ._constants import _SDK_MODULE_PREFIX

_CONTRACT_BASE_NAMES: frozenset[str] = frozenset({"Input", "Output"})

# Builtin bytes-like types that carry the same Temporal payload-size risk.
_BYTES_LIKE_NAMES: frozenset[str] = frozenset({"bytes", "bytearray", "memoryview"})


def _base_name(node: ast.expr) -> str | None:
    """Return the terminal name of a base expression (``Input`` / ``mod.Output``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def collect_foreign_contract_names(tree: ast.AST) -> frozenset[str]:
    """Return local names bound to ``Input``/``Output`` imported from non-SDK modules.

    E.g. ``from pydantic_ai import Output`` → ``frozenset({"Output"})``.
    Used to avoid false-positives when apps use third-party types with the same
    terminal name.

    **Relative imports are skipped** (``from . import Output``,
    ``from .contracts import Output``).  These are app-internal re-exports that
    almost always wrap the SDK's own ``Output``; tagging them as foreign would
    silently suppress P011/P012 on every class in that file.

    **Absolute non-SDK imports are deliberately treated as foreign** (e.g.
    ``from strawberry import Output``).  An app that imports a third-party
    ``Output`` and re-exports it under an absolute path accepts that P011/P012
    will be silent for classes extending that name; they should use relative
    imports (``from . import Output``) to avoid this.
    """
    foreign: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # Relative imports (from . / from .sub) are app-internal re-exports;
        # skip them so we don't suppress checks on the files that use them.
        if node.level > 0:
            continue
        module = node.module or ""
        is_sdk = module == _SDK_MODULE_PREFIX or module.startswith(
            _SDK_MODULE_PREFIX + "."
        )
        if not is_sdk:
            for alias in node.names:
                if alias.name in _CONTRACT_BASE_NAMES:
                    foreign.add(alias.asname or alias.name)
    return frozenset(foreign)


def is_contract_class(
    node: ast.ClassDef, foreign_names: frozenset[str] = frozenset()
) -> bool:
    """True if any base name is in :data:`_CONTRACT_BASE_NAMES` and not in *foreign_names*."""
    return any(
        _base_name(base) in _CONTRACT_BASE_NAMES
        and _base_name(base) not in foreign_names
        for base in node.bases
    )


def iter_contract_classes(
    tree: ast.AST, foreign_names: frozenset[str] = frozenset()
) -> Iterator[ast.ClassDef]:
    """Yield every ``ClassDef`` in *tree* that looks like a contract class."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and is_contract_class(node, foreign_names):
            yield node


def _is_name(node: ast.expr, target: str) -> bool:
    return isinstance(node, ast.Name) and node.id == target


def _is_none(node: ast.expr) -> bool:
    # ``None`` parses as ``ast.Constant(value=None)`` on Python 3.8+, but tolerate
    # a bare ``Name("None")`` for robustness.
    return (isinstance(node, ast.Constant) and node.value is None) or _is_name(
        node, "None"
    )


def _is_optional_union(node: ast.expr, target: str) -> bool:
    """True for ``target | None`` (or ``None | target``) PEP 604 unions."""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
        return False
    left, right = node.left, node.right
    return (_is_name(left, target) and _is_none(right)) or (
        _is_name(right, target) and _is_none(left)
    )


def _is_optional_subscript(node: ast.expr, target: str) -> bool:
    """True for ``Optional[target]``."""
    if not isinstance(node, ast.Subscript):
        return False
    if not _is_name(node.value, "Optional"):
        return False
    return _is_name(node.slice, target)


def _is_annotation(node: ast.expr, target: str) -> bool:
    return (
        _is_name(node, target)
        or _is_optional_union(node, target)
        or _is_optional_subscript(node, target)
    )


def bytes_type_name(node: ast.expr) -> str | None:
    """Return the matched bytes-like type name, or ``None`` if not a bytes-like annotation."""
    for name in _BYTES_LIKE_NAMES:
        if _is_annotation(node, name):
            return name
    return None


def is_bytes_annotation(node: ast.expr) -> bool:
    """True for ``bytes``/``bytearray``/``memoryview`` (bare, ``| None``, or ``Optional[…]``)."""
    return bytes_type_name(node) is not None


def is_str_annotation(node: ast.expr) -> bool:
    """True for ``str``, ``str | None``, ``Optional[str]``."""
    return _is_annotation(node, "str")


def is_workflow_path_annotation(node: ast.expr) -> bool:
    """True for ``WorkflowPath``, ``WorkflowPath | None``, ``Optional[WorkflowPath]``.

    ``WorkflowPath`` (``application_sdk.contracts.types``) is a ``str`` alias for a
    deterministic, worker-portable workflow-relative path.  It is the sanctioned,
    self-documenting alternative to a bare ``str`` path field and is exempt from
    P012.
    """
    return _is_annotation(node, "WorkflowPath")


# ── P015 unmodeled-container helpers ──────────────────────────────────────────

# Container type names that, when used as a field annotation on a contract
# *without* a nested model as the element/value type, indicate a stringly-typed
# or otherwise unmodeled boundary payload (P015 UnmodeledBoundedContractField).
_CONTAINER_NAMES: frozenset[str] = frozenset(
    {
        # dict-like
        "dict",
        "Dict",
        "Mapping",
        "MutableMapping",
        # list-like
        "list",
        "List",
        "Sequence",
        "MutableSequence",
        # set-like
        "set",
        "Set",
        "FrozenSet",
        "frozenset",
    }
)

# Sub-set of container names that take (K, V) type params (dict-like). All
# others in _CONTAINER_NAMES take a single element type param (list/set-like).
_DICT_LIKE_NAMES: frozenset[str] = frozenset(
    {"dict", "Dict", "Mapping", "MutableMapping"}
)

# Type names that, when used as the element/value parameter of a container,
# mark the container as *unmodeled* (primitives and Any).
_PRIMITIVE_NAMES: frozenset[str] = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "Any",
        "object",
    }
)


def _terminal_name(node: ast.expr) -> str | None:
    """Return the terminal (rightmost) name of a (possibly subscripted) annotation.

    ``dict[str, str]`` → ``"dict"``;  ``List[Any]`` → ``"List"``;
    ``mod.Output`` → ``"Output"``;  ``None`` literal → ``"None"``.
    Returns ``None`` for unrecognised forms.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _terminal_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    # ``-> None`` parses as ast.Constant(value=None) on Python 3.8+.
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return None


def _unwrap_annotated(node: ast.expr) -> ast.expr:
    """Peer through ``Annotated[X, ...]`` to the inner type ``X``.

    Returns *node* unchanged if it is not an ``Annotated`` subscript.
    """
    if not isinstance(node, ast.Subscript):
        return node
    if not _is_name(node.value, "Annotated"):
        return node
    slc = node.slice
    # Annotated[X, metadata, ...] — first element of the slice Tuple is X.
    if isinstance(slc, ast.Tuple) and slc.elts:
        return slc.elts[0]
    return node


def _unwrap_optional_node(node: ast.expr) -> ast.expr:
    """Peer through ``Optional[X]`` / ``X | None`` / ``None | X`` to the inner type.

    Returns *node* unchanged if it is not an optional wrapper.
    """
    # X | None  (PEP 604)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _is_none(node.right):
            return node.left
        if _is_none(node.left):
            return node.right
    # Optional[X]
    if isinstance(node, ast.Subscript) and _is_name(node.value, "Optional"):
        return node.slice
    return node


def _container_element_type(node: ast.Subscript) -> ast.expr | None:
    """Return the element / value type of a subscripted container node.

    For dict-like containers (``dict[K, V]``) returns the *value* type ``V``
    (the last element of the slice Tuple) — the key type is structural metadata
    and does not determine whether the payload is modeled.  For list/set-like
    containers (``list[T]``, ``set[T]``) returns ``T``.

    Returns ``None`` when the slice structure is not as expected (e.g. bare
    unsubscripted container with no type params).
    """
    container_name = _terminal_name(node.value)
    slc = node.slice
    if container_name in _DICT_LIKE_NAMES:
        if isinstance(slc, ast.Tuple) and len(slc.elts) >= 2:
            return slc.elts[-1]  # value type V
        if not isinstance(slc, ast.Tuple):
            return slc  # degenerate single-param form
        return None
    else:
        # list/set-like: single element type
        if isinstance(slc, ast.Tuple):
            return slc.elts[0] if slc.elts else None
        return slc  # directly T


def unmodeled_container_name(node: ast.expr) -> str | None:
    """Return the container name if *node* is an unmodeled-container annotation.

    An annotation is an *unmodeled container* when it:

    * names a container type (``dict``, ``list``, ``set`` and their
      :mod:`typing`-module equivalents, ``Mapping``, ``Sequence``, etc.);
    * **and** is bare (no type parameters) **or** is parametrised with a
      primitive / ``Any`` element/value type (``dict[str, str]``,
      ``list[str]``, ``Annotated[dict[str, Any], MaxItems(50)]``).

    Containers of a typed class (``list[FooModel]``, ``dict[str, FooModel]``)
    are **exempt** — a collection of models is the canonical bounded pattern
    and does not need to be replaced.

    ``Annotated[X, ...]`` wrappers (e.g. ``Annotated[dict[…], MaxItems(N)]``)
    are transparent: the check peers through them to the inner type ``X``
    before deciding.  ``Optional[X]`` / ``X | None`` are similarly unwrapped.

    Returns the container name (e.g. ``"dict"``, ``"list"``) for the finding
    message, or ``None`` when the annotation is not an unmodeled container.
    """
    # Peer through Annotated[X, ...] and Optional[X] / X | None to a fixed point.
    # A single pass misses Optional[Annotated[...]] when Annotated is the outer wrapper.
    inner = node
    while True:
        unwrapped = _unwrap_annotated(inner)
        unwrapped = _unwrap_optional_node(unwrapped)
        if unwrapped is inner:
            break
        inner = unwrapped

    # Bare container name: dict, list, set, … — always unmodeled.
    if isinstance(inner, ast.Name) and inner.id in _CONTAINER_NAMES:
        return inner.id

    # Subscripted container: dict[K, V], list[T], etc.
    if isinstance(inner, ast.Subscript):
        container_name = _terminal_name(inner.value)
        if container_name not in _CONTAINER_NAMES:
            return None
        elem = _container_element_type(inner)
        if elem is None:
            # Unresolvable slice structure → treat as unmodeled.
            return container_name
        if isinstance(elem, ast.Name):
            if elem.id in _PRIMITIVE_NAMES:
                return container_name  # container of primitives → unmodeled
            # Non-primitive class name → assumed a nested model → exempt.
            return None
        # Complex element type (e.g. list[dict[str,str]], list[Optional[str]])
        # → treat as unmodeled (conservative).
        return container_name

    return None


def field_doc_text(classdef: ast.ClassDef, ann_node: ast.AnnAssign) -> str:
    """Return documentation text for a field, or ``""`` if none is present.

    Two sources are recognised:

    * A ``Field(description="...")`` default call on the ``AnnAssign``.
    * A PEP-257 attribute docstring: a bare string ``Expr`` statement
      immediately following the ``AnnAssign`` in the class body.
    """
    # Field(description="...") default.
    value = ann_node.value
    if isinstance(value, ast.Call):
        terminal = value.func
        is_field = (isinstance(terminal, ast.Name) and terminal.id == "Field") or (
            isinstance(terminal, ast.Attribute) and terminal.attr == "Field"
        )
        if is_field:
            for kw in value.keywords:
                if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        return kw.value.value

    # PEP-257 attribute docstring on the immediately-following statement.
    body = classdef.body
    for idx, stmt in enumerate(body):
        if stmt is ann_node:
            nxt = body[idx + 1] if idx + 1 < len(body) else None
            if (
                isinstance(nxt, ast.Expr)
                and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)
            ):
                return nxt.value.value
            break
    return ""

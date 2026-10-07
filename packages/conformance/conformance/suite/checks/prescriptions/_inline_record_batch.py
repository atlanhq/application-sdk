"""P055 InlineRecordBatchAcrossBoundary — records must not ride the activity payload.

A collection of records (``list[dict[...]]``, ``list[Any]``, ``Sequence[Mapping]``…)
declared on a task boundary travels inside the Temporal payload, which is capped
(2 MiB by the SDK, 4 MiB by gRPC). The count can be bounded with ``MaxItems``;
the size of each record cannot, so the batch passes every test and fails the
first time a tenant's backlog is large. The run then dies on its first task and
nothing it fetched is ever processed.

Two shapes are checked, because P001 sees neither once it is silenced or
bypassed:

* a field on an ``Input``/``Output`` subclass (the opt-out shape — P001 fires on
  ``allow_unbounded_fields`` but accepts an inline suppression);
* a parameter or return of a raw ``@activity.defn`` function (no SDK contract,
  so payload-safety validation never runs).

The finding is deliberately NOT suppressible: there is no batch size at which
an inline record list is safe, and the fix (a ``FileReference``) is always
available. Matching is by annotation only and stays conservative — a list of
scalars or of a named model is not flagged.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "P055"

_CONTRACT_BASES = frozenset({"Input", "Output"})

#: Collection types whose element type decides whether the value is a record batch.
_SEQUENCE_TYPES = frozenset(
    {
        "list",
        "List",
        "Sequence",
        "MutableSequence",
        "tuple",
        "Tuple",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
        "Iterable",
        "Collection",
    }
)

#: Element types that make each item an unbounded record.
_RECORD_TYPES = frozenset({"dict", "Dict", "Mapping", "MutableMapping", "Any"})

_FIX = (
    "Write the records to a local file and hand on "
    "FileReference.from_local(path, tier=StorageTier.TRANSIENT): the activity "
    "interceptor persists it to the object store, materialises it for the next "
    "task, and cleanup_storage removes it at run end. Only the ref and counts "
    "should cross the boundary."
)


def _name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _unwrap_annotated(node: ast.expr) -> ast.expr:
    while isinstance(node, ast.Subscript) and _name(node.value) == "Annotated":
        inner = node.slice
        node = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
    return node


def _union_members(node: ast.expr) -> list[ast.expr]:
    """``A | B``, ``Optional[A]`` and ``Union[A, B]`` → their members."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_members(node.left) + _union_members(node.right)
    if isinstance(node, ast.Subscript) and _name(node.value) in {"Optional", "Union"}:
        inner = node.slice
        members = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [m for member in members for m in _union_members(member)]
    return [node]


def _is_record(node: ast.expr) -> bool:
    node = _unwrap_annotated(node)
    if isinstance(node, ast.Subscript):
        return _name(node.value) in _RECORD_TYPES
    return _name(node) in _RECORD_TYPES


def _is_record_batch(annotation: ast.expr | None) -> bool:
    """True when *annotation* is a collection whose elements are records."""
    if annotation is None:
        return False
    for member in _union_members(_unwrap_annotated(annotation)):
        member = _unwrap_annotated(member)
        if not isinstance(member, ast.Subscript):
            continue
        if _name(member.value) not in _SEQUENCE_TYPES:
            continue
        element = member.slice
        if isinstance(element, ast.Tuple):
            # tuple[X, ...] / tuple[X, Y]: any record element makes it a batch.
            if any(_is_record(e) for e in element.elts if not _is_ellipsis(e)):
                return True
            continue
        if _is_record(element):
            return True
    return False


def _is_ellipsis(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is Ellipsis


def _is_contract_subclass(node: ast.ClassDef) -> bool:
    return any(_name(base) in _CONTRACT_BASES for base in node.bases)


def _is_activity_defn(decorator: ast.expr) -> bool:
    """``@activity.defn``, ``@activity.defn(...)`` or a bare imported ``@defn``."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr == "defn" and _name(target.value) == "activity"
    return isinstance(target, ast.Name) and target.id == "defn"


def _finding(filename: str, node: ast.AST, message: str) -> Finding:
    # directives={}: not suppressible (see module docstring).
    return make_finding(
        filename=filename,
        rule_id=_RULE_ID,
        node=node,
        message=message,
        directives={},
    )


def _check_contract(node: ast.ClassDef, filename: str) -> list[Finding]:
    findings: list[Finding] = []
    for stmt in node.body:
        if not (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and _is_record_batch(stmt.annotation)
        ):
            continue
        findings.append(
            _finding(
                filename,
                stmt,
                f"Contract '{node.name}' field '{stmt.target.id}' carries a batch of "
                "records inline across the task boundary. MaxItems bounds the count, "
                "not each record's size, so the payload outgrows Temporal's limit on "
                f"the first large backlog and the run fails. {_FIX}",
            )
        )
    return findings


def _check_activity(
    node: ast.FunctionDef | ast.AsyncFunctionDef, filename: str
) -> list[Finding]:
    findings: list[Finding] = []
    args = node.args
    params = args.posonlyargs + args.args + args.kwonlyargs
    for arg in params:
        if _is_record_batch(arg.annotation):
            findings.append(
                _finding(
                    filename,
                    arg,
                    f"Activity '{node.name}' takes a batch of records inline "
                    f"('{arg.arg}'); it arrives in the Temporal payload, which is "
                    f"size-capped. {_FIX}",
                )
            )
    if _is_record_batch(node.returns):
        findings.append(
            _finding(
                filename,
                node,
                f"Activity '{node.name}' returns a batch of records inline; the "
                "result is size-capped by Temporal, and an oversized completion "
                f"fails or times out every attempt. {_FIX}",
            )
        )
    return findings


def check_p055(tree: ast.AST, filename: str) -> list[Finding]:
    """Emit P055 findings for one module. Suppression directives are ignored."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_contract_subclass(node):
            findings.extend(_check_contract(node, filename))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _is_activity_defn(d) for d in node.decorator_list
        ):
            findings.extend(_check_activity(node, filename))
    return findings

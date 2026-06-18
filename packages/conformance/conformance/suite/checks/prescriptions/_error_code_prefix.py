"""P003 ErrorCodePrefixMismatch — cross-file inheritance checker.

Every concrete subclass of an ``application_sdk.errors`` leaf must declare its
own ``code: ClassVar[str]`` starting with the leaf's category prefix + ``_``.
Resolution is transitive and cross-file so intermediate pass-through classes
are also caught.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.error_handling._helpers import _get_name
from conformance.suite.schema.findings import Finding

# Mirrors application_sdk/errors/leaves.py.  Subclasses (transitively) of these
# 14 classes must declare a ``code: ClassVar[str]`` that starts with the
# corresponding prefix followed by an underscore.
LEAF_PREFIX_MAP: dict[str, str] = {
    "CancelledError": "CANCELLED",
    "AppTimeoutError": "TIMEOUT",
    "RateLimitedError": "RATE_LIMITED",
    "AuthError": "AUTH",
    "AppPermissionDeniedError": "PERMISSION",
    "NotFoundError": "NOT_FOUND",
    "AlreadyExistsError": "ALREADY_EXISTS",
    "InvalidInputError": "INVALID_INPUT",
    "PreconditionError": "PRECONDITION",
    "DependencyUnavailableError": "DEPENDENCY_UNAVAILABLE",
    "ResourceExhaustedError": "RESOURCE_EXHAUSTED",
    "DataIntegrityError": "DATA_INTEGRITY",
    "InternalError": "INTERNAL",
    "UnimplementedError": "UNIMPLEMENTED",
}

# Inverted map so messages can cite the leaf class name alongside the prefix.
_PREFIX_TO_LEAF: dict[str, str] = {v: k for k, v in LEAF_PREFIX_MAP.items()}


@dataclass
class ClassRecord:
    """A ClassDef collected for P003 inheritance resolution."""

    name: str
    file: str
    node: ast.ClassDef
    bases: list[str] = field(default_factory=list)
    code_value: str | None = None
    code_node: ast.AST | None = None


def _is_classvar_annotation(annotation: ast.expr | None) -> bool:
    """True if ``annotation`` is ``ClassVar`` or ``ClassVar[...]`` (any form)."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Subscript):
        return _get_name(annotation.value) == "ClassVar"
    return _get_name(annotation) == "ClassVar"


def _extract_code(cls_node: ast.ClassDef) -> tuple[str | None, ast.AST | None]:
    """Find the class-level ``code`` literal assignment, if any.

    Accepts both ``code: ClassVar[str] = "..."`` (the prescribed form) and the
    plain ``code = "..."`` shorthand.
    """
    for stmt in cls_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id != "code":
                continue
            if not _is_classvar_annotation(stmt.annotation):
                continue
            if (
                stmt.value is not None
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                return stmt.value.value, stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "code"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    return stmt.value.value, stmt
    return None, None


def collect_import_aliases(tree: ast.Module) -> dict[str, str]:
    """Return per-file ``{local_name: original_name}`` for ``from X import Y [as Z]``.

    Maps local identifiers to the original imported name so aliased leaf
    imports (``from … import InternalError as _InternalError``) are resolved
    to the real leaf name during class registry lookup.
    """
    aliases: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            local = alias.asname if alias.asname else alias.name
            aliases[local] = alias.name
    return aliases


def collect_classes(
    tree: ast.AST, rel_file: str, aliases: dict[str, str]
) -> list[ClassRecord]:
    """Walk *tree* and return one record per ClassDef.

    Base names are de-aliased through *aliases* so alias-imported leaves are
    recognised during transitive resolution.
    """
    records: list[ClassRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases: list[str] = []
        for base in node.bases:
            n = _get_name(base)
            if n is None:
                continue
            bases.append(aliases.get(n, n))
        code_value, code_node = _extract_code(node)
        records.append(
            ClassRecord(
                name=node.name,
                file=rel_file,
                node=node,
                bases=bases,
                code_value=code_value,
                code_node=code_node,
            )
        )
    return records


def resolve_leaf_prefix(
    name: str,
    by_name: dict[str, ClassRecord],
    cache: dict[str, str | None],
    visiting: set[str],
) -> str | None:
    """Walk the (transitive) base chain of *name* and return the leaf prefix.

    Returns ``None`` if *name* is not derived from one of the 14 leaves.
    Cycle-safe via ``visiting``; results memoised in ``cache``.
    """
    if name in LEAF_PREFIX_MAP:
        return LEAF_PREFIX_MAP[name]
    if name in cache:
        return cache[name]
    if name in visiting:
        return None
    rec = by_name.get(name)
    if rec is None:
        cache[name] = None
        return None
    visiting.add(name)
    result: str | None = None
    for base in rec.bases:
        prefix = resolve_leaf_prefix(base, by_name, cache, visiting)
        if prefix is not None:
            result = prefix
            break
    visiting.discard(name)
    cache[name] = result
    return result


def emit_p003(
    rec: ClassRecord,
    leaf_prefix: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    """Build a P003 finding for *rec* (missing or wrong-prefix code)."""
    leaf_class = _PREFIX_TO_LEAF.get(leaf_prefix, leaf_prefix)
    if rec.code_value is None:
        node: ast.AST = rec.node
        message = (
            f"Class '{rec.name}' is a (transitive) subclass of '{leaf_class}' "
            f"(category prefix '{leaf_prefix}_') but does not declare its own "
            f"'code: ClassVar[str]'.  Without an override, every raise of this class "
            f"collapses to the bare leaf code '{leaf_prefix}', making the failure "
            f"impossible to triage from dashboards.  "
            f"Add a code that starts with '{leaf_prefix}_' (typed-error-prescription §4).  "
            f"See https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p003"
        )
    else:
        node = rec.code_node or rec.node
        message = (
            f"Error code '{rec.code_value}' on class '{rec.name}' must start with the "
            f"parent leaf's category prefix '{leaf_prefix}_' (subclass of "
            f"'{leaf_class}').  The category prefix lets dashboards and on-call routing "
            f"group by failure category without joining on the category column "
            f"(typed-error-prescription §4).  "
            f"See https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p003"
        )
    return make_finding(
        filename=rec.file,
        rule_id="P003",
        node=node,
        message=message,
        directives=directives,
    )

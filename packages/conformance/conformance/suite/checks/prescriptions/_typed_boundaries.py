"""P013 UntypedEntrypointBoundary / P014 UntypedTaskBoundary — boundary-type enforcement.

Every ``@entrypoint`` method (P013) and ``@task`` method (P014) on an App must
declare a properly-typed input parameter (a class that transitively subclasses
the SDK's ``Input``) and return type (a class that transitively subclasses
``Output``).

Violations caught:

1. **Missing annotation** — the non-``self`` param or return has no type annotation.
2. **Clearly untyped** — the annotation is a builtin/typing primitive or container
   (``dict``, ``list``, ``Any``, ``str``, ``None``, etc.), including subscripted/bounded
   forms (``dict[str, str]``) — these never subclass ``Input``/``Output``.
3. **Resolvable but wrong base** — the annotation class IS in the scanned source
   tree (or importable from within the app) but does **not** transitively subclass
   ``Input``/``Output`` (e.g. a plain ``pydantic.BaseModel`` subclass, a dataclass,
   or an unrelated class).

FP-avoidance: a class name that is *not* in the scanned tree and is *not* a
clearly-untyped annotation is assumed OK (third-party / generated code we cannot
follow). Only names resolvable within the scanned universe are flagged.

P013 also covers the **implicit ``run()`` entrypoint**: a method named ``run``
on a class whose base chain transitively reaches ``App`` is treated as an
implicit entrypoint and checked under P013.

Decorator provenance
--------------------
Provenance gating is shared with P008 via ``_decorator_provenance``: a decorator
is only treated as the SDK ``task`` / ``entrypoint`` when the name is *not*
explicitly imported from a non-SDK module.  ``@celery.task``, ``@flask.entrypoint``,
and similar third-party decorators are excluded.

Cross-file resolution
---------------------
Mirrors the P003 multi-pass pattern: ``check_p013_p014`` operates on the
pre-built ``by_name`` registry assembled by ``scan_all`` and resolves base
chains transitively and cycle-safely using ``resolve_ancestor`` from
``_error_code_prefix``.  Results are memoised per-call.

Alias resolution
----------------
``collect_import_aliases`` de-aliases annotation class names before resolution
so that ``from .contracts import FetchInput as MyAlias`` is resolved correctly.
Module-level ``import application_sdk.contracts as c`` is handled by the
``sdk_contract_module_aliases`` provenance set: a ``c.FetchInput`` annotation is
fast-pathed as valid without needing to be in the scanned tree.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import _terminal_name, _unwrap_annotated, _unwrap_optional_node
from ._decorator_provenance import (
    ImportProvenance,
    collect_import_provenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from ._error_code_prefix import ClassRecord, collect_import_aliases, resolve_ancestor

# ── Constants ──────────────────────────────────────────────────────────────────

# Annotations whose terminal name falls in this set are *always* a violation —
# they can never be a subclass of Input/Output regardless of type parameters.
_CLEARLY_UNTYPED: frozenset[str] = frozenset(
    {
        # primitives
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "object",
        # typing wilds
        "Any",
        "None",
        "NoneType",
        # containers (bare or subscripted)
        "dict",
        "Dict",
        "list",
        "List",
        "tuple",
        "Tuple",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "MutableSequence",
        "Iterable",
        "Collection",
        "Iterator",
        "Generator",
        "Callable",
        # typing extras that are never contracts
        "ClassVar",
        "Final",
        "Literal",
        "Union",
        "Type",
        "type",
    }
)

# ── Annotation analysis ───────────────────────────────────────────────────────


def _annotation_terminal_name(node: ast.expr | None) -> str | None:
    """Return the terminal class name of an annotation, unwrapping all wrappers.

    Unwraps ``Optional[X]`` / ``X | None`` and ``Annotated[X, ...]`` in a
    fixed-point loop so nested forms like ``Optional[Annotated[dict, M]]`` and
    ``Annotated[Optional[dict]]`` are fully reduced before the terminal name is
    read.

    ``FetchInput`` → ``"FetchInput"``; ``Optional[FetchInput]`` → ``"FetchInput"``;
    ``Optional[Annotated[dict[str,str], M]]`` → ``"dict"``;
    ``None`` literal (``-> None``) → ``"None"``.
    """
    if node is None:
        return None
    # Peer through Optional[X] / X | None and Annotated[X, ...] to a fixed point.
    inner = node
    while True:
        unwrapped = _unwrap_optional_node(inner)
        unwrapped = _unwrap_annotated(unwrapped)
        if unwrapped is inner:
            break
        inner = unwrapped
    return _terminal_name(inner)


def _is_clearly_untyped_annotation(node: ast.expr | None) -> bool:
    """True when the annotation is clearly untyped (primitive, container, or Any).

    Covers bare names (``dict``), subscripted forms (``dict[str, str]``), and
    ``Optional``/union wrappers around them.  A ``None`` (missing) annotation is
    also a violation — the caller is responsible for the finding message.
    """
    if node is None:
        return False  # caller handles missing annotation separately
    name = _annotation_terminal_name(node)
    return name is not None and name in _CLEARLY_UNTYPED


# ── Method iteration ──────────────────────────────────────────────────────────


def _iter_class_body_methods(
    class_node: ast.ClassDef,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield direct (non-nested) method defs from *class_node*'s body."""
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield stmt


def _get_non_self_params(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.arg]:
    """Return the non-``self``/non-``cls`` parameters of a method.

    Combines positional-only (``/``), regular, and keyword-only (``*``)
    parameters so that ``def f(self, *, input: dict)`` and
    ``def f(self, input: dict, /)`` are both handled correctly.
    """
    all_params = (
        list(func.args.posonlyargs) + list(func.args.args) + list(func.args.kwonlyargs)
    )
    if all_params and all_params[0].arg in ("self", "cls"):
        return all_params[1:]
    return all_params


# ── Finding helpers ───────────────────────────────────────────────────────────


def _make_boundary_finding(
    *,
    rule_id: str,
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    ann_node: ast.expr | None,
    fallback_node: ast.AST,
    direction: str,
    ann_name: str,
    reason: str,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    """Construct a P013/P014 boundary finding with a canonical message."""
    target_base = "Input" if direction == "input" else "Output"
    if direction == "input":
        guidance = (
            "Define a class inheriting from Input "
            "(application_sdk.contracts) and use it as the parameter type."
        )
    else:
        guidance = (
            "Define a class inheriting from Output "
            "(application_sdk.contracts) and use it as the return type."
        )
    message = (
        f"Method '{func.name}' {direction} annotation '{ann_name}' {reason}. "
        f"Boundary types must subclass the SDK's {target_base} "
        f"(application_sdk.contracts). {guidance} "
        f"Suppress with '# conformance: ignore[{rule_id}] <reason>' on the "
        "def line (the violation node is the annotation, not the call-site)."
    )
    node: ast.AST = ann_node if ann_node is not None else fallback_node
    return make_finding(
        filename=filename,
        rule_id=rule_id,
        node=node,
        message=message,
        directives=directives,
    )


def _check_one_annotation(
    *,
    annotation: ast.expr | None,
    fallback_node: ast.AST,
    target_base: str,
    direction: str,
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rule_id: str,
    filename: str,
    directives: dict[int, _IgnoreDirective],
    by_name: dict[str, ClassRecord],
    sdk_contract_imports: frozenset[str],
    sdk_contract_module_aliases: frozenset[str],
    aliases: dict[str, str],
    cache: dict[str, bool | None],
) -> Finding | None:
    """Return a finding for *annotation* when it is not a valid contract, else ``None``."""
    # ── Case 1: missing annotation ────────────────────────────────────────────
    if annotation is None:
        target_base_name = "Input" if direction == "input" else "Output"
        node: ast.AST = fallback_node
        message = (
            f"Method '{func.name}' {direction} is missing a type annotation. "
            f"Boundary types must subclass the SDK's {target_base_name} "
            "(application_sdk.contracts). "
            f"Define a class inheriting from {target_base_name} and annotate "
            "the parameter/return accordingly. "
            f"Suppress with '# conformance: ignore[{rule_id}] <reason>' on the "
            "def line (the violation node is the annotation, not the call-site)."
        )
        return make_finding(
            filename=filename,
            rule_id=rule_id,
            node=node,
            message=message,
            directives=directives,
        )

    # ── Case 2: clearly untyped ───────────────────────────────────────────────
    if _is_clearly_untyped_annotation(annotation):
        ann_name = _annotation_terminal_name(annotation) or "<untyped>"
        return _make_boundary_finding(
            rule_id=rule_id,
            func=func,
            ann_node=annotation,
            fallback_node=fallback_node,
            direction=direction,
            ann_name=ann_name,
            reason="is a primitive, container, or untyped structure",
            filename=filename,
            directives=directives,
        )

    # ── Case 3a: module-level SDK contract alias (import ... as c; c.FetchInput) ─
    # Unwrap Optional so that Optional[c.FetchInput] is also handled.
    inner_ann = _unwrap_optional_node(annotation)
    if (
        isinstance(inner_ann, ast.Attribute)
        and isinstance(inner_ann.value, ast.Name)
        and inner_ann.value.id in sdk_contract_module_aliases
    ):
        return None  # valid by provenance

    # ── Resolve annotation class name ─────────────────────────────────────────
    ann_name = _annotation_terminal_name(annotation)
    if ann_name is None:
        # Unrecognisable annotation form (e.g. complex subscript) → skip.
        return None

    # De-alias so that `from .contracts import FetchInput as MyAlias` resolves.
    ann_name = aliases.get(ann_name, ann_name)

    # ── Case 3b: SDK contract import by provenance (from ... import X) → valid ─
    if ann_name in sdk_contract_imports:
        return None

    # ── Case 3c: cross-file resolution ───────────────────────────────────────
    result = resolve_ancestor(ann_name, target_base, by_name, cache, set())
    if result is False:
        # Found in scanned universe but does not reach Input/Output.
        return _make_boundary_finding(
            rule_id=rule_id,
            func=func,
            ann_node=annotation,
            fallback_node=fallback_node,
            direction=direction,
            ann_name=ann_name,
            reason=f"does not subclass the SDK's {'Input' if direction == 'input' else 'Output'}",
            filename=filename,
            directives=directives,
        )

    # result is True (valid) or None (unresolvable → assumed OK) → no finding.
    return None


# ── Main entry point ──────────────────────────────────────────────────────────


def check_p013_p014(
    file_trees: dict[Path, ast.AST],
    by_name: dict[str, ClassRecord],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P013/P014 for entrypoint/task methods with untyped boundary types.

    Operates on the pre-built ``by_name`` class registry and ``file_trees``
    collected during the Pass 1 scan in ``scan_all`` — both must cover the full
    set of Python source files so cross-file inheritance resolution is complete.

    P013 fires for ``@entrypoint`` methods and for implicit ``run()`` overrides
    on ``App`` subclasses.  P014 fires for ``@task`` methods.  Each rule checks
    both the input parameter annotation and the return annotation.
    """
    findings: list[Finding] = []

    # Memoised resolution caches shared across all files (keyed by class name).
    input_cache: dict[str, bool | None] = {}
    output_cache: dict[str, bool | None] = {}
    app_cache: dict[str, bool | None] = {}

    for path, tree in file_trees.items():
        directives = file_directives.get(path, {})
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        filename = str(rel)

        prov: ImportProvenance = collect_import_provenance(tree)
        # Per-file import aliases for de-aliasing annotation class names
        # (e.g. `from .contracts import FetchInput as MyAlias`).
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            for func in _iter_class_body_methods(class_node):
                # ── Determine which rule applies ──────────────────────────────
                rule_id: str | None = None

                if any(
                    is_entrypoint_decorator(dec, prov) for dec in func.decorator_list
                ):
                    rule_id = "P013"

                elif any(is_task_decorator(dec, prov) for dec in func.decorator_list):
                    rule_id = "P014"

                elif func.name == "run" and isinstance(func, ast.AsyncFunctionDef):
                    # Implicit entrypoint: async run() on an App subclass.
                    # Only async — App.run() is defined as async-only; a sync
                    # def run() would be rejected by the runtime before P013
                    # has any value, so we intentionally skip it here.
                    for base in class_node.bases:
                        base_name: str | None = None
                        if isinstance(base, ast.Name):
                            base_name = base.id
                        elif isinstance(base, ast.Attribute):
                            base_name = base.attr
                        if base_name is None:
                            continue
                        # De-alias before comparing: handles `App as BaseApp`.
                        base_name = aliases.get(base_name, base_name)
                        if (
                            base_name == "App"
                            or resolve_ancestor(
                                base_name, "App", by_name, app_cache, set()
                            )
                            is True
                        ):
                            rule_id = "P013"
                            break

                if rule_id is None:
                    continue

                # ── Get the non-self input parameter ─────────────────────────
                non_self = _get_non_self_params(func)
                if not non_self:
                    # Wrong arity — the runtime decorator will reject this.
                    continue
                input_param = non_self[0]

                # ── Check input annotation ────────────────────────────────────
                finding = _check_one_annotation(
                    annotation=input_param.annotation,
                    fallback_node=input_param,
                    target_base="Input",
                    direction="input",
                    func=func,
                    rule_id=rule_id,
                    filename=filename,
                    directives=directives,
                    by_name=by_name,
                    sdk_contract_imports=prov.sdk_contract_names,
                    sdk_contract_module_aliases=prov.sdk_contract_module_aliases,
                    aliases=aliases,
                    cache=input_cache,
                )
                if finding is not None:
                    findings.append(finding)

                # ── Check return annotation ───────────────────────────────────
                finding = _check_one_annotation(
                    annotation=func.returns,
                    fallback_node=func,
                    target_base="Output",
                    direction="output",
                    func=func,
                    rule_id=rule_id,
                    filename=filename,
                    directives=directives,
                    by_name=by_name,
                    sdk_contract_imports=prov.sdk_contract_names,
                    sdk_contract_module_aliases=prov.sdk_contract_module_aliases,
                    aliases=aliases,
                    cache=output_cache,
                )
                if finding is not None:
                    findings.append(finding)

    return findings

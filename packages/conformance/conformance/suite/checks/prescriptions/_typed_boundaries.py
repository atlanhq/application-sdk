"""P013 UntypedEntrypointBoundary / P014 UntypedTaskBoundary — boundary-type enforcement.

Every ``@entrypoint`` method (P013) and ``@task`` method (P014) on an App must
declare a properly-typed input parameter (a class that transitively subclasses
the SDK's ``Input``) and return type (a class that transitively subclasses
``Output``).

Violations caught:

1. **Missing annotation** — the non-``self`` param or return has no type annotation.
2. **Clearly untyped** — the annotation is a builtin/typing primitive or container
   (``dict``, ``list``, ``Any``, ``str``, etc.), including subscripted/bounded forms
   (``dict[str, str]``) — these never subclass ``Input``/``Output``.
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
Mirrors ``_framework_transfer.py``: a decorator is only treated as the SDK
``task`` / ``entrypoint`` when the name is *not* explicitly imported from a
non-SDK module.  ``@celery.task``, ``@invoke.task``, and similar third-party
task decorators are excluded.

Cross-file resolution
---------------------
Mirrors the P003 multi-pass pattern: ``check_p013_p014`` operates on the
pre-built ``by_name`` registry assembled by ``scan_all`` and resolves base
chains transitively and cycle-safely.  Results are memoised per-call.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import _terminal_name, _unwrap_optional_node
from ._error_code_prefix import ClassRecord

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

_SDK_MODULE_PREFIX = "application_sdk"

# Imports from these module prefixes are valid contracts by provenance —
# skip cross-file resolution and treat them as correctly typed.
_SDK_CONTRACT_MODULE_PREFIXES: tuple[str, ...] = (
    "application_sdk.contracts",
    "application_sdk.handler.contracts",
)

# ── Provenance helpers ─────────────────────────────────────────────────────────


def _collect_ep_task_provenance(
    tree: ast.AST,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """Collect import-provenance sets needed for decorator and annotation gating.

    Returns
    -------
    non_sdk_task_names :
        Local names bound to ``task`` from a non-SDK ``from … import task``.
    non_sdk_ep_names :
        Local names bound to ``entrypoint`` from a non-SDK import.
    non_sdk_module_names :
        Local names of imported non-SDK top-level modules (``import celery``).
    sdk_contract_imports :
        Local names imported from ``application_sdk.contracts.*`` or
        ``application_sdk.handler.contracts.*`` — valid by provenance.
    """
    non_sdk_task: set[str] = set()
    non_sdk_ep: set[str] = set()
    non_sdk_mods: set[str] = set()
    sdk_contracts: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            is_sdk = module == _SDK_MODULE_PREFIX or module.startswith(
                _SDK_MODULE_PREFIX + "."
            )
            if not is_sdk:
                for alias in node.names:
                    if alias.name == "task":
                        non_sdk_task.add(alias.asname or alias.name)
                    if alias.name == "entrypoint":
                        non_sdk_ep.add(alias.asname or alias.name)
            # SDK contract provenance: any import from application_sdk.contracts.*
            # or application_sdk.handler.contracts.* is treated as a valid contract.
            if is_sdk and any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _SDK_CONTRACT_MODULE_PREFIXES
            ):
                for alias in node.names:
                    sdk_contracts.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                is_sdk = name == _SDK_MODULE_PREFIX or name.startswith(
                    _SDK_MODULE_PREFIX + "."
                )
                if not is_sdk:
                    non_sdk_mods.add(alias.asname or name.split(".")[0])

    return (
        frozenset(non_sdk_task),
        frozenset(non_sdk_ep),
        frozenset(non_sdk_mods),
        frozenset(sdk_contracts),
    )


# ── Decorator helpers ─────────────────────────────────────────────────────────


def _is_task_decorator(
    dec: ast.expr,
    non_sdk_task_names: frozenset[str],
    non_sdk_module_names: frozenset[str],
) -> bool:
    """True if *dec* is the SDK ``@task`` decorator (not a third-party one)."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id == "task" and dec.id not in non_sdk_task_names
    if isinstance(dec, ast.Attribute):
        return dec.attr == "task" and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in non_sdk_module_names
        )
    return False


def _is_entrypoint_decorator(
    dec: ast.expr,
    non_sdk_ep_names: frozenset[str],
    non_sdk_module_names: frozenset[str],
) -> bool:
    """True if *dec* is the SDK ``@entrypoint`` decorator (not a third-party one)."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id == "entrypoint" and dec.id not in non_sdk_ep_names
    if isinstance(dec, ast.Attribute):
        return dec.attr == "entrypoint" and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in non_sdk_module_names
        )
    return False


# ── Annotation analysis ───────────────────────────────────────────────────────


def _annotation_terminal_name(node: ast.expr | None) -> str | None:
    """Return the terminal class name of an annotation, unwrapping Optional/union.

    ``FetchInput`` → ``"FetchInput"``; ``FetchInput | None`` → ``"FetchInput"``;
    ``Optional[FetchInput]`` → ``"FetchInput"``; ``dict[str, str]`` → ``"dict"``;
    ``None`` (missing annotation) → ``None``.
    """
    if node is None:
        return None
    # Peer through Optional[X] and X | None.
    inner = _unwrap_optional_node(node)
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


# ── Cross-file resolution ─────────────────────────────────────────────────────


def _resolve_to_base(
    name: str,
    target: str,
    by_name: dict[str, ClassRecord],
    cache: dict[str, bool | None],
    visiting: set[str],
) -> bool | None:
    """Transitively resolve *name*'s inheritance chain looking for *target*.

    Returns
    -------
    ``True``
        *name* or one of its ancestors IS *target* (valid boundary type).
    ``False``
        *name* is in the scanned universe but does *not* reach *target*
        (violation — the class is a concrete wrong-base type).  This is
        definitive even when some ancestor bases are external/unknown: an
        external base that is not in *by_name* simply does not contribute
        a confirmed path to *target*, so it is treated as non-reaching.
    ``None``
        *name* itself is not in the scanned universe (unknown / third-party
        / generated — assumed OK to avoid false positives).
    """
    if name == target:
        return True
    if name in cache:
        return cache[name]
    if name in visiting:
        # Cycle detected — treat as unknown to avoid false positives.
        return None
    rec = by_name.get(name)
    if rec is None:
        # Not in the scanned source tree → cannot determine → assume OK.
        cache[name] = None
        return None
    visiting.add(name)
    # Class IS in the scanned universe → result is definitive (True or False).
    # We only return None when the class itself is absent from by_name (line above).
    # An external/unknown *base* (sub is None) does not make the class's status
    # unknown — it simply means that base doesn't confirm the target, so we
    # continue looking at other bases, and if none resolve to True, the class
    # does NOT reach the target (result stays False → violation).
    result: bool = False
    for base in rec.bases:
        sub = _resolve_to_base(base, target, by_name, cache, visiting)
        if sub is True:
            result = True
            break
        # sub is None (external base) or False (doesn't reach target) — either
        # way this base doesn't establish the target; try the next one.
    visiting.discard(name)
    cache[name] = result
    return result


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
    """Return the non-``self``/non-``cls`` parameters of a method."""
    args = func.args.args
    if args and args[0].arg in ("self", "cls"):
        return args[1:]
    return list(args)


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
        f"(application_sdk.contracts). {guidance}"
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
            "the parameter/return accordingly."
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

    # ── Resolve annotation class name ─────────────────────────────────────────
    ann_name = _annotation_terminal_name(annotation)
    if ann_name is None:
        # Unrecognisable annotation form (e.g. complex subscript) → skip.
        return None

    # ── Case 3a: SDK contract import by provenance → valid ───────────────────
    if ann_name in sdk_contract_imports:
        return None

    # ── Case 3b: cross-file resolution ───────────────────────────────────────
    result = _resolve_to_base(ann_name, target_base, by_name, cache, set())
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

        non_sdk_task, non_sdk_ep, non_sdk_mods, sdk_contracts = (
            _collect_ep_task_provenance(tree)
        )

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            for func in _iter_class_body_methods(class_node):
                # ── Determine which rule applies ──────────────────────────────
                rule_id: str | None = None

                if any(
                    _is_entrypoint_decorator(dec, non_sdk_ep, non_sdk_mods)
                    for dec in func.decorator_list
                ):
                    rule_id = "P013"

                elif any(
                    _is_task_decorator(dec, non_sdk_task, non_sdk_mods)
                    for dec in func.decorator_list
                ):
                    rule_id = "P014"

                elif func.name == "run" and isinstance(func, ast.AsyncFunctionDef):
                    # Implicit entrypoint: run() on an App subclass.
                    for base in class_node.bases:
                        base_name: str | None = None
                        if isinstance(base, ast.Name):
                            base_name = base.id
                        elif isinstance(base, ast.Attribute):
                            base_name = base.attr
                        if base_name is None:
                            continue
                        if (
                            base_name == "App"
                            or _resolve_to_base(
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
                    sdk_contract_imports=sdk_contracts,
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
                    sdk_contract_imports=sdk_contracts,
                    cache=output_cache,
                )
                if finding is not None:
                    findings.append(finding)

    return findings

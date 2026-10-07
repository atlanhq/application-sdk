"""P003 ErrorCodePrefixMismatch — cross-file inheritance checker.

Every concrete subclass of an ``application_sdk.errors`` leaf must declare its
own ``code: ClassVar[str]`` starting with the leaf's category prefix + ``_``.
Resolution is transitive and cross-file so intermediate pass-through classes
are also caught.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    make_finding,
    non_sdk_import_roots,
    sdk_app_base_bindings,
)
from conformance.suite.checks.error_handling._helpers import _get_name
from conformance.suite.schema.findings import Finding

# Mirrors application_sdk/errors/leaves.py.  Subclasses (transitively) of these
# 15 classes must declare a ``code: ClassVar[str]`` that starts with the
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
    "SourceUnavailableError": "SOURCE_UNAVAILABLE",
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
    overrides_emission: bool = False
    sdk_app_bases: frozenset[str] = frozenset()
    """Entries of :attr:`bases` that the defining module binds to an SDK
    ``App``-family class by an absolute ``from application_sdk… import``.

    Import provenance is file-local, so it is captured here, where the class is
    defined; a scan of a subclass in another file cannot recover it from the
    bare base name."""
    non_sdk_bases: Mapping[str, str | None] = field(default_factory=dict)
    """Entries of :attr:`bases` that the defining module binds by a ``from …
    import`` of a module outside ``application_sdk``, each mapped to that
    module's top-level package (``None`` for a relative import)."""
    base_modules: Mapping[str, str] = field(default_factory=dict)
    """Entries of :attr:`bases` mapped to the dotted module the defining module
    imports them from (``from m import X``, ``import m`` + ``m.X``), relative
    imports resolved against the defining file."""


# The ONE method that, when overridden, takes the emitted code out of ``code``'s
# hands. ``AppError.to_failure_details()`` builds the ``FailureDetails`` wire
# envelope and is what puts ``code`` in front of a dashboard; a class that
# replaces it supplies its own code by another route, so demanding a prefix on
# ``code`` is meaningless there.
#
# ``qualified_code`` is deliberately NOT in this set. It is only the
# log-line/human-readable surface — a class that overrides it while leaving
# ``to_failure_details`` alone still emits the bare leaf code on the wire, which
# is exactly the harm P003 exists to catch.
EMISSION_OVERRIDES: frozenset[str] = frozenset({"to_failure_details"})

# ``AppError.to_failure_details`` is the *default* envelope, not a replacement.
# When this repo is scanned, that method is in the class registry, and walking
# every ancestor would credit every leaf subclass with an exemption — hollowing
# P003 on dogfood. The 15 leaves inherit that default; they are stops too.
# Mixins (not in these sets) still propagate.
_EMISSION_STOPS: frozenset[str] = frozenset({"AppError"}) | frozenset(LEAF_PREFIX_MAP)


def _overrides_emission(cls_node: ast.ClassDef) -> bool:
    """True if *cls_node* defines its own code-emission method."""
    return any(
        isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and stmt.name in EMISSION_OVERRIDES
        for stmt in cls_node.body
    )


def resolve_emission_override(
    name: str,
    by_name: dict[str, ClassRecord],
    cache: dict[str, bool],
    visiting: set[str],
) -> bool:
    """True if *name* or a non-leaf mixin ancestor overrides code emission.

    Mixins that carry ``to_failure_details`` are credited to every class that
    mixes them in. ``AppError`` and the 15 leaves are resolution stops: their
    method is the default envelope, not a replacement, so inheriting from them
    must not buy the exemption. Bases outside the scanned tree cannot be
    inspected and simply do not confirm an override.
    """
    if name in cache:
        return cache[name]
    if name in visiting:
        return False
    rec = by_name.get(name)
    if rec is None:
        cache[name] = False
        return False
    visiting.add(name)
    result = rec.overrides_emission or any(
        resolve_emission_override(base, by_name, cache, visiting)
        for base in rec.bases
        if base not in _EMISSION_STOPS
    )
    visiting.discard(name)
    cache[name] = result
    return result


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


def module_code_constants(tree: ast.AST) -> dict[str, str]:
    """Map module-level code constants to their string value.

    Two shapes, both used by connector apps to keep their codes in one
    module and reference them from the exception classes:

    * ``NAME = "literal"``            -> ``{"NAME": "literal"}``
    * ``NAME = Code(code="literal")`` -> ``{"NAME.code": "literal"}``

    Only module-level assignments with a literal ``str`` are recorded, so a
    value the scanner cannot prove stays unresolved (and P003 keeps firing
    conservatively).
    """
    consts: dict[str, str] = {}
    if not isinstance(tree, ast.Module):
        return consts
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts.setdefault(target.id, value.value)
        elif isinstance(value, ast.Call):
            for kw in value.keywords:
                if (
                    kw.arg == "code"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    consts.setdefault(f"{target.id}.code", kw.value.value)
    return consts


def _indirect_code_key(value: ast.expr) -> str | None:
    """Lookup key for a non-literal ``code`` value, or None if unsupported.

    ``codes.FT_AUTH_NO_TOKEN.code`` and ``FT_AUTH_NO_TOKEN.code`` both key on
    ``FT_AUTH_NO_TOKEN.code``; a bare ``CODE`` keys on ``CODE``. The module
    path in front is ignored on purpose: the constant map is app-wide, and
    the constant name alone is what identifies it.
    """
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        owner = value.value
        if isinstance(owner, ast.Name):
            return f"{owner.id}.{value.attr}"
        if isinstance(owner, ast.Attribute):
            return f"{owner.attr}.{value.attr}"
    return None


def resolve_indirect_codes(records: list[ClassRecord], consts: dict[str, str]) -> None:
    """Fill in ``code_value`` for classes whose ``code`` is an indirection.

    A class that writes ``code: ClassVar[str] = codes.X.code`` DOES declare
    its code — the scanner simply could not read it, so P003 reported "does
    not declare" and, worse, would keep firing after the app fixed the string.
    Resolving the indirection turns those into an accurate wrong-prefix
    finding, or silence when the prefix is right. Values that stay
    unresolvable are left alone and keep firing conservatively.
    """
    for rec in records:
        if rec.code_value is not None:
            continue
        for stmt in rec.node.body:
            value: ast.expr | None = None
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == "code"
                and _is_classvar_annotation(stmt.annotation)
            ):
                value = stmt.value
            elif isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "code" for t in stmt.targets
            ):
                value = stmt.value
            if value is None or isinstance(value, ast.Constant):
                continue
            key = _indirect_code_key(value)
            resolved = consts.get(key) if key else None
            if resolved is not None:
                rec.code_value = resolved
                rec.code_node = stmt
            break


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


def _absolute_module(node: ast.ImportFrom, rel_file: str) -> str | None:
    if node.level == 0:
        return node.module or None
    package = rel_file.replace("\\", "/").split("/")[:-1]
    if node.level - 1 > len(package):
        return None
    package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])]) or None


def _single_bindings(tree: ast.AST) -> set[str]:
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names = [node.id]
        elif isinstance(node, ast.alias):
            names = [node.asname or node.name.split(".")[0]]
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        else:
            continue
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return {name for name, count in counts.items() if count == 1}


def _import_modules(
    tree: ast.AST, rel_file: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Per-file ``{local: module}`` for imported names and for imported modules.

    Only module-level imports of names the file binds exactly once count.
    """
    name_modules: dict[str, str] = {}
    module_bindings: dict[str, str] = {}
    if not isinstance(tree, ast.Module):
        return name_modules, module_bindings
    single = _single_bindings(tree)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, rel_file)
            if module is None:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if local not in single:
                    continue
                name_modules[local] = module
                module_bindings[local] = (
                    f"{module}.{alias.name}" if module else alias.name
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) not in single:
                    continue
                if alias.asname:
                    module_bindings[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    module_bindings[root] = root
    return name_modules, module_bindings


def _base_module(
    base: ast.expr, name_modules: dict[str, str], module_bindings: dict[str, str]
) -> str | None:
    if isinstance(base, ast.Name):
        return name_modules.get(base.id)
    if not isinstance(base, ast.Attribute):
        return None
    parts: list[str] = []
    value: ast.expr = base.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name) or value.id not in module_bindings:
        return None
    return ".".join([module_bindings[value.id], *reversed(parts)])


def _defines_module(rec: ClassRecord, module: str) -> bool:
    if rec.node.col_offset != 0:
        return False
    path = rec.file.replace("\\", "/").removesuffix(".py").removesuffix("/__init__")
    dotted = path.replace("/", ".")
    return dotted in (module, f"src.{module}")


def collect_classes(
    tree: ast.AST, rel_file: str, aliases: dict[str, str]
) -> list[ClassRecord]:
    """Walk *tree* and return one record per ClassDef.

    Base names are de-aliased through *aliases* so alias-imported leaves are
    recognised during transitive resolution.
    """
    records: list[ClassRecord] = []
    sdk_bindings = sdk_app_base_bindings(tree)
    foreign_roots = non_sdk_import_roots(tree)
    name_modules, module_bindings = _import_modules(tree, rel_file)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases: list[str] = []
        sdk_app_bases: set[str] = set()
        non_sdk_bases: dict[str, str | None] = {}
        base_modules: dict[str, str] = {}
        for base in node.bases:
            n = _get_name(base)
            if n is None:
                continue
            bases.append(aliases.get(n, n))
            if isinstance(base, ast.Name) and base.id in sdk_bindings:
                sdk_app_bases.add(bases[-1])
            if isinstance(base, ast.Name) and base.id in foreign_roots:
                non_sdk_bases[bases[-1]] = foreign_roots[base.id]
            module = _base_module(base, name_modules, module_bindings)
            if module:
                base_modules[bases[-1]] = module
        code_value, code_node = _extract_code(node)
        records.append(
            ClassRecord(
                name=node.name,
                file=rel_file,
                node=node,
                bases=bases,
                code_value=code_value,
                code_node=code_node,
                overrides_emission=_overrides_emission(node),
                sdk_app_bases=frozenset(sdk_app_bases),
                non_sdk_bases=non_sdk_bases,
                base_modules=base_modules,
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

    Returns ``None`` if *name* is not derived from one of the 15 leaves.
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


def _shadowed_base_reaches(
    base_name: str,
    rec: ClassRecord,
    target: str,
    by_name: dict[str, ClassRecord],
    cache: dict[str, bool | None],
    visiting: set[str],
    known_targets: frozenset[str],
    known_ancestors: frozenset[str],
    by_name_all: Mapping[str, Sequence[ClassRecord]] | None,
) -> bool:
    """Whether the one top-level class *base_name* in the module *rec* imports it
    from reaches *target*.

    Walks with a private copy of *cache*, so nothing it learns leaks into the
    shared memo.
    """
    module = rec.base_modules.get(base_name)
    if module is None:
        return False
    candidates = [
        other
        for other in (by_name_all or {}).get(base_name, ())
        if _defines_module(other, module)
    ]
    if len(candidates) != 1:
        return False
    cache = dict(cache)
    for other in candidates:
        for base in other.bases:
            if (
                resolve_ancestor(
                    base,
                    target,
                    by_name,
                    cache,
                    visiting,
                    known_targets,
                    known_ancestors,
                    by_name_all,
                )
                is True
            ):
                return True
    return False


def resolve_ancestor(
    name: str,
    target: str,
    by_name: dict[str, ClassRecord],
    cache: dict[str, bool | None],
    visiting: set[str],
    known_targets: frozenset[str] = frozenset(),
    known_ancestors: frozenset[str] = frozenset(),
    by_name_all: Mapping[str, Sequence[ClassRecord]] | None = None,
) -> bool | None:
    """Transitively resolve *name*'s base chain looking for *target*.

    Generic counterpart to :func:`resolve_leaf_prefix` with ``bool | None``
    semantics for use by P013/P014 boundary resolution.

    Returns
    -------
    ``True``
        *name* IS *target*, or one of its ancestors is.
    ``False``
        *name* is in the scanned universe but none of its ancestors reach
        *target*.  Definitive even when some bases are external/unknown — an
        external base simply fails to confirm the target.
    ``None``
        *name* is not in the scanned universe (unknown / third-party /
        generated — assumed OK to avoid false positives).

    *by_name_all* holds every record per bare name. When a class subclasses a
    same-named class, the record of that name defined in the module the base is
    imported from is walked with a private memo; if it reaches *target*, the
    result is ``True``.
    """
    if name == target or name in known_targets:
        return True
    if name in known_ancestors and name not in by_name:
        return name.endswith(target)
    # Provenance is file-local, so do not reuse a cache entry produced without
    # this set of known SDK contract names.
    if not known_targets and name in cache:
        return cache[name]
    if name in visiting:
        # Cycle — treat as unknown to avoid false positives.
        return None
    rec = by_name.get(name)
    if rec is None:
        cache[name] = None
        return None
    visiting.add(name)
    result: bool = False
    same_name_base = False
    for base in rec.bases:
        if base == name:
            # A same-named class from another module: resolve it by import
            # provenance, else the chain is unresolvable, not negative.
            if _shadowed_base_reaches(
                base,
                rec,
                target,
                by_name,
                cache,
                visiting,
                known_targets,
                known_ancestors,
                by_name_all,
            ):
                result = True
                break
            same_name_base = True
            continue
        sub = resolve_ancestor(
            base,
            target,
            by_name,
            cache,
            visiting,
            known_targets,
            known_ancestors,
            by_name_all,
        )
        if sub is True:
            result = True
            break
        # sub is None (external base) or False — keep looking.
    visiting.discard(name)
    if not result and same_name_base:
        # Unknown, not "does not subclass": firing here is a false positive on a
        # class that shadows its own generated base (the standard toolkit shape).
        if not known_targets:
            cache[name] = None
        return None
    if not known_targets:
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

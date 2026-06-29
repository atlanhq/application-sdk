"""Collect App-family subclass names from Python source files via AST.

The SDK App-family bases that consumer code may subclass are:

* ``App``                          (``application_sdk.app``)
* ``SqlApp``                       (``application_sdk.templates``)
* ``BaseMetadataExtractor``        (``application_sdk.templates``)
* ``IncrementalSqlMetadataExtractor`` (``application_sdk.templates``)
* ``SqlMetadataExtractor``         (``application_sdk.templates``)
* ``SqlQueryExtractor``            (``application_sdk.templates``)

Any import from ``application_sdk.*`` whose ``name`` is in the set above is
tracked as an App-family alias.  This is the import-provenance guard: only classes
that inherit from a *locally* imported SDK base are considered.

Name-resolution mirrors ``App.__init_subclass__`` in
``application_sdk/app/base.py`` exactly:

* Explicit ``name = "literal"`` or ``name: ClassVar[str] = "literal"`` in the
  class body → the literal.
* No ``name`` attr → ``_pascal_to_kebab(class_name)``.
* ``name = <non-literal>`` (variable, f-string, …) → *unverifiable*.

Leaf detection
--------------
A class is a *leaf* when no other scanned class uses its name as a direct base.
Leaf detection happens in two phases to support within-file (and cross-file)
intermediate base classes:

1. **Direct phase:** classes whose bases include at least one imported SDK alias.
2. **Transitive phase:** classes whose bases include any name already identified
   as app-family (repeated until no new classes are added).

Leaf = app-family AND class_name not in the set of bases used by any other
scanned class.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

_SDK_PREFIX = "application_sdk"

_SDK_APP_BASE_NAMES: frozenset[str] = frozenset(
    {
        "App",
        "SqlApp",
        "BaseMetadataExtractor",
        "IncrementalSqlMetadataExtractor",
        "SqlMetadataExtractor",
        "SqlQueryExtractor",
    }
)


# ---------------------------------------------------------------------------
# Pascal → kebab  (copied verbatim from application_sdk/app/base.py)
# ---------------------------------------------------------------------------


def _pascal_to_kebab(name: str) -> str:
    """Convert PascalCase to kebab-case.

    Examples:
        Greeter -> greeter
        CsvPipeline -> csv-pipeline
        MyAwesomeApp -> my-awesome-app
        HTTPHandler -> http-handler
        S3Loader -> s3-loader
        MSSQLMetadataExtractor -> mssql-metadata-extractor
    """
    # Handle consecutive uppercase (like HTTP -> http-)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    # Handle lowercase followed by uppercase (like my -> my-)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", s)
    return s.lower()


# ---------------------------------------------------------------------------
# Import-provenance helper
# ---------------------------------------------------------------------------


def _sdk_app_aliases(tree: ast.Module) -> frozenset[str]:
    """Return local names bound to SDK App-family base classes in this module.

    Tracks any ``from application_sdk.<sub>... import <SDKBase> [as alias]``
    where ``<SDKBase>`` is in :data:`_SDK_APP_BASE_NAMES`.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level > 0:
            continue
        module = node.module or ""
        if module == _SDK_PREFIX or module.startswith(_SDK_PREFIX + "."):
            for alias in node.names:
                if alias.name in _SDK_APP_BASE_NAMES:
                    bound.add(alias.asname or alias.name)
    return frozenset(bound)


# ---------------------------------------------------------------------------
# Name-resolution helper
# ---------------------------------------------------------------------------


def _resolve_class_name(
    class_def: ast.ClassDef,
) -> tuple[str | None, bool, ast.AST]:
    """Resolve the app name for an App-family class definition.

    Returns
    -------
    (resolved_name, is_unresolvable, anchor_node):
        ``resolved_name``    – the string name; ``None`` when unresolvable.
        ``is_unresolvable``  – ``True`` when a ``name`` attr exists but is
                               non-literal (so the caller should emit an
                               unverifiable finding).
        ``anchor_node``      – the best AST node to anchor a finding to:
                               the assignment statement when ``name = ...`` is
                               present, or the ``ClassDef`` node itself when
                               the name is derived from the class name.
    """
    for stmt in class_def.body:
        # name = "..."  or  name: ClassVar[str] = "..."
        target_name: str | None = None
        value_node: ast.expr | None = None

        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "name":
                    target_name = "name"
                    value_node = stmt.value
                    break

        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "name":
                target_name = "name"
                value_node = stmt.value  # may be None for declarations

        if target_name is None:
            continue

        if value_node is not None and isinstance(value_node, ast.Constant):
            if isinstance(value_node.value, str):
                if value_node.value:  # non-empty literal → use as-is
                    return value_node.value, False, stmt
                # Empty string: runtime treats "" as falsy and falls back to
                # kebab(class.__name__) — mirror that here by breaking to the
                # kebab derivation below rather than returning "".
                break
        # Present but non-literal (or annotation-only with no value)
        if value_node is not None:
            return None, True, stmt

    # No name attr (or empty-string literal) → derive from class name
    return _pascal_to_kebab(class_def.name), False, class_def


def _resolve_ancestor_name(
    base_names: list[str],
    name_to_raw: dict[str, "_RawClassDef"],
    app_family_names: set[str],
) -> tuple[str | None, bool] | None:
    """BFS through scanned app-family ancestors for the nearest body-level ``name``.

    Used when a leaf class has no body-level ``name`` attr of its own, so the
    runtime would resolve ``cls.name`` via MRO through the ancestor chain.  Only
    classes in the scanned set (``app_family_names``) are visited; SDK-imported
    bases are not in the scanned raw list and are therefore skipped.

    Returns
    -------
    ``(literal_name, False)``
        A non-empty string literal was found on an ancestor.
    ``(None, True)``
        A non-literal (or empty-literal that breaks the chain) was found first.
    ``None``
        No scanned ancestor has a body-level ``name`` attr — use kebab fallback.
    """
    seen: set[str] = set()
    queue = list(base_names)
    while queue:
        base_name = queue.pop(0)
        if base_name in seen or base_name not in app_family_names:
            continue
        seen.add(base_name)
        ancestor = name_to_raw.get(base_name)
        if ancestor is None:
            continue
        resolved_name, is_unresolvable, name_node = _resolve_class_name(ancestor.node)
        if name_node is not ancestor.node:
            # Ancestor has a body-level name attr (literal or non-literal)
            return resolved_name, is_unresolvable
        # No effective body-level name in this ancestor — continue BFS upward
        queue.extend(ancestor.base_names)
    return None


# ---------------------------------------------------------------------------
# Per-file collection record
# ---------------------------------------------------------------------------


@dataclass
class _RawClassDef:
    """Intermediate record collected during the per-file scan pass."""

    filename: str
    node: ast.ClassDef
    base_names: list[str]
    """Direct base class names as simple identifier strings.

    Only ``Name``-form bases (``class Foo(Bar)``) are captured; ``Attribute``-form
    bases (``class Foo(pkg.Bar)``) are stored as the attribute chain
    (``"pkg.Bar"``), but are NOT matched against the app-family set — they never
    originate from an SDK alias, which is an import-provenance guard by design.
    """
    is_sdk_direct: bool
    """True when at least one base is a known SDK app-family alias in this file."""


# ---------------------------------------------------------------------------
# Result data structures (public API for _check.py)
# ---------------------------------------------------------------------------


@dataclass
class AppClassInfo:
    """An App-family leaf class found in the codebase."""

    class_name: str
    """The Python class name."""

    resolved_name: str | None
    """The SDK-derived app name (``None`` when unresolvable)."""

    filename: str
    """Repo-relative path to the source file."""

    node: ast.ClassDef
    """The class definition AST node — the primary suppression anchor."""

    name_node: ast.AST
    """The ``name = "..."`` statement node (or the ``ClassDef`` itself when
    the name is kebab-derived from the class name)."""


@dataclass
class CodeAppNameScan:
    """Accumulated results of scanning all Python files for App-family leaf classes."""

    resolved: list[AppClassInfo] = field(default_factory=list)
    """Leaf App-family classes with a statically resolved name."""

    unresolvable: list[AppClassInfo] = field(default_factory=list)
    """Leaf App-family classes whose ``name`` attribute is a non-literal expression."""


# ---------------------------------------------------------------------------
# Per-file scanner
# ---------------------------------------------------------------------------


def scan_file(
    tree: ast.Module,
    filename: str,
    raw: list[_RawClassDef],
) -> None:
    """Scan one parsed module, appending raw class records to *raw*.

    Files without any SDK app-family import contribute no records (import-
    provenance guard: avoids false positives from coincidentally-named bases).
    """
    sdk_aliases = _sdk_app_aliases(tree)
    if not sdk_aliases:
        return  # No SDK app-family import in this file — skip.

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        base_names: list[str] = []
        is_sdk_direct = False

        for base in node.bases:
            if isinstance(base, ast.Name):
                base_names.append(base.id)
                if base.id in sdk_aliases:
                    is_sdk_direct = True
            elif isinstance(base, ast.Attribute):
                # e.g. module.App — not an alias → store dotted form but never
                # mark as sdk_direct (aliases are always simple names)
                base_names.append(f"{getattr(base.value, 'id', '?')}.{base.attr}")

        raw.append(
            _RawClassDef(
                filename=filename,
                node=node,
                base_names=base_names,
                is_sdk_direct=is_sdk_direct,
            )
        )


# ---------------------------------------------------------------------------
# Cross-file post-processing
# ---------------------------------------------------------------------------


def resolve_leaf_classes(raw: list[_RawClassDef]) -> CodeAppNameScan:
    """From the raw cross-file class list, derive the set of leaf App-family classes.

    Three-phase algorithm
    ---------------------
    1. **Direct phase:** seed ``app_family_names`` with classes that directly
       subclass an SDK alias in their own file.
    2. **Transitive phase:** iteratively add classes whose base name appears in
       ``app_family_names`` (handles local intermediate base classes).
    3. **Leaf phase:** remove any class whose name is itself used as a base by
       another scanned class.

    Name resolution is applied only to leaf classes.
    """
    # Phase 1 — direct SDK bases
    app_family_names: set[str] = {r.node.name for r in raw if r.is_sdk_direct}

    # Phase 2 — transitive (within the scanned set)
    changed = True
    while changed:
        changed = False
        for r in raw:
            if r.node.name in app_family_names:
                continue
            if any(b in app_family_names for b in r.base_names):
                app_family_names.add(r.node.name)
                changed = True

    # Phase 3 — leaf detection
    bases_used: set[str] = {b for r in raw for b in r.base_names}
    leaf_records = [
        r
        for r in raw
        if r.node.name in app_family_names and r.node.name not in bases_used
    ]

    # Build name→raw map for ancestor name lookup (used below)
    name_to_raw: dict[str, _RawClassDef] = {r.node.name: r for r in raw}

    # Name resolution for leaves
    result = CodeAppNameScan()
    for r in leaf_records:
        resolved_name, is_unresolvable, name_node = _resolve_class_name(r.node)

        # When the leaf has no body-level ``name`` (name_node is the ClassDef
        # itself — i.e. the kebab fallback path), walk scanned app-family
        # ancestors for the nearest body-level name.  This mirrors how
        # ``cls.name`` resolves via MRO at runtime: an intermediate base that
        # declares ``name = "foo"`` propagates to all subclasses that don't
        # override it, so the static check must do the same to avoid emitting
        # false-positive drift findings.
        if name_node is r.node:
            ancestor_result = _resolve_ancestor_name(
                r.base_names, name_to_raw, app_family_names
            )
            if ancestor_result is not None:
                resolved_name, is_unresolvable = ancestor_result

        info = AppClassInfo(
            class_name=r.node.name,
            resolved_name=resolved_name,
            filename=r.filename,
            node=r.node,
            name_node=name_node,
        )
        if is_unresolvable:
            result.unresolvable.append(info)
        else:
            result.resolved.append(info)

    return result

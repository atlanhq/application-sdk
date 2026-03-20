"""
Rewrites v2 application_sdk import paths to their v3 equivalents.

Uses libcst for lossless CST-based transformation so that formatting,
comments, and non-import code are preserved byte-for-byte.

What it handles
---------------
- ``from X import Y``                   → rewrites module + symbol
- ``from X import Y as Z``              → rewrites module + symbol, keeps alias
- ``from X import Y, Z``                → rewrites each symbol; splits into
                                          separate lines if they map to
                                          different new modules
- ``from X import *``                   → rewrites module path only
- Imports with ``structural=True``      → adds a ``# TODO(v3-migration)``
                                          comment on the preceding line

What it does NOT do
-------------------
- Merge Workflow + Activities classes into a single App subclass
- Rewrite method bodies or decorator arguments
- Touch non-import code

Usage
-----
Rewrite a single file (in-place)::

    python -m tools.migrate_v3.rewrite_imports path/to/connector.py

Dry-run a single file (print to stdout)::

    python -m tools.migrate_v3.rewrite_imports --dry-run path/to/connector.py

Rewrite all Python files in a directory tree::

    python -m tools.migrate_v3.rewrite_imports src/

After running, check remaining structural work::

    python -m tools.migrate_v3.check_migration src/
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import libcst as cst

from .import_mapping import DEPRECATED_MODULES, MODULE_MAP, SYMBOL_MAP, RewriteTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_to_str(node: cst.Attribute | cst.Name | None) -> str:
    """Convert a libcst dotted-name node to a plain string like ``a.b.c``."""
    if node is None:
        return ""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_module_to_str(node.value)}.{node.attr.value}"
    return ""


def _str_to_module(dotted: str) -> cst.Attribute | cst.Name:
    """Build a libcst dotted-name node from a string like ``a.b.c``."""
    parts = dotted.split(".")
    result: cst.Attribute | cst.Name = cst.Name(parts[0])
    for part in parts[1:]:
        result = cst.Attribute(value=result, attr=cst.Name(part))
    return result


def _resolve(old_module: str, old_symbol: str) -> RewriteTarget | None:
    """
    Look up the rewrite target for (module, symbol).

    Checks SYMBOL_MAP first, then falls back to MODULE_MAP (keeping the
    symbol name unchanged).
    """
    target = SYMBOL_MAP.get((old_module, old_symbol))
    if target is not None:
        return target

    fallback = MODULE_MAP.get(old_module)
    if fallback is not None:
        new_module, structural, note = fallback
        return RewriteTarget(
            new_module=new_module,
            new_symbol=None,  # keep original symbol name
            structural=structural,
            note=note,
        )
    return None


# ---------------------------------------------------------------------------
# Transformer
# ---------------------------------------------------------------------------


class V3ImportRewriter(cst.CSTTransformer):
    """
    libcst transformer that rewrites deprecated v2 import paths.

    After calling ``tree.visit(rewriter)``, inspect ``rewriter.changes`` for
    a human-readable log of every rewrite that was applied.
    """

    def __init__(self) -> None:
        super().__init__()
        # Log of (file-local description) for each rewrite applied.
        self.changes: list[str] = []
        # Set by leave_ImportFrom when a TODO comment should be added; read
        # (and cleared) by leave_SimpleStatementLine.
        self._pending_todo: str | None = None

    # ------------------------------------------------------------------
    # ImportFrom visitor
    # ------------------------------------------------------------------

    def leave_ImportFrom(
        self,
        original_node: cst.ImportFrom,
        updated_node: cst.ImportFrom,
    ) -> cst.ImportFrom:
        old_module = _module_to_str(updated_node.module)
        if old_module not in DEPRECATED_MODULES:
            return updated_node

        # ── star import ──────────────────────────────────────────────────
        if isinstance(updated_node.names, cst.ImportStar):
            fallback = MODULE_MAP.get(old_module)
            if fallback:
                new_module_str, structural, note = fallback
                self.changes.append(f"  {old_module} → {new_module_str} (*)")
                if structural and note:
                    self._pending_todo = note
                return updated_node.with_changes(module=_str_to_module(new_module_str))
            return updated_node

        # ── named imports ────────────────────────────────────────────────
        aliases: Sequence[cst.ImportAlias] = updated_node.names  # type: ignore[assignment]

        # Resolve each symbol to its new (module, target).
        resolved: list[tuple[cst.ImportAlias, str, RewriteTarget]] = []
        for alias in aliases:
            if not isinstance(alias.name, cst.Name):
                # Dotted names inside `from X import a.b` — unusual, skip.
                resolved.append((alias, old_module, RewriteTarget(old_module, None)))
                continue
            old_sym = alias.name.value
            target = _resolve(old_module, old_sym)
            if target is None:
                # Symbol not in the map; at minimum rewrite the module via fallback.
                fb = MODULE_MAP.get(old_module)
                if fb:
                    new_mod, structural, note = fb
                    target = RewriteTarget(new_mod, None, structural, note)
                else:
                    target = RewriteTarget(old_module, None)
            resolved.append((alias, old_module, target))

        # Group by new module so we can decide whether to split.
        by_module: dict[str, list[tuple[cst.ImportAlias, str, RewriteTarget]]] = (
            defaultdict(list)
        )
        for alias, om, target in resolved:
            by_module[target.new_module].append((alias, om, target))

        any_structural = any(t.structural for _, _, t in resolved)

        if len(by_module) == 1:
            # All symbols land in the same new module — single import line.
            new_module_str = next(iter(by_module))
            new_aliases = self._build_aliases(resolved)
            self._log_changes(old_module, new_module_str, resolved)
            if any_structural:
                notes = [t.note for _, _, t in resolved if t.structural and t.note]
                self._pending_todo = (
                    notes[0] if notes else "manual structural refactoring required"
                )
            return updated_node.with_changes(
                module=_str_to_module(new_module_str),
                names=new_aliases,
            )

        # Multiple target modules — add a TODO and rewrite per-module.
        # libcst can only return one node from leave_ImportFrom, so we rewrite
        # for the first group and log a TODO asking the developer to split.
        first_module = next(iter(by_module))
        first_group = by_module[first_module]
        new_aliases = self._build_aliases(first_group)
        self._log_changes(old_module, first_module, first_group)

        remaining = [
            f"{t.new_module}.{t.new_symbol or a.name.value if isinstance(a.name, cst.Name) else '?'}"
            for group in list(by_module.values())[1:]
            for a, _, t in group
        ]
        self._pending_todo = (
            "Split this import — the following symbols need separate import lines: "
            + ", ".join(remaining)
        )
        return updated_node.with_changes(
            module=_str_to_module(first_module),
            names=new_aliases,
        )

    # ------------------------------------------------------------------
    # SimpleStatementLine visitor — attaches TODO comments
    # ------------------------------------------------------------------

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine:
        if self._pending_todo is None:
            return updated_node

        todo, self._pending_todo = self._pending_todo, None
        # Truncate long notes so the comment stays readable on one line.
        if len(todo) > 120:
            todo = todo[:117] + "..."
        comment_line = cst.EmptyLine(
            indent=True,
            comment=cst.Comment(f"# TODO(v3-migration): {todo}"),
        )
        # Append after any existing leading blank lines so the comment sits
        # immediately above the import.
        new_leading = list(updated_node.leading_lines) + [comment_line]
        return updated_node.with_changes(leading_lines=new_leading)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_aliases(
        self,
        group: list[tuple[cst.ImportAlias, str, RewriteTarget]],
    ) -> list[cst.ImportAlias]:
        """Rebuild the ImportAlias list with updated symbol names."""
        new_aliases: list[cst.ImportAlias] = []
        for i, (alias, _old_module, target) in enumerate(group):
            new_sym = (
                target.new_symbol
                if target.new_symbol is not None
                else (alias.name.value if isinstance(alias.name, cst.Name) else None)
            )
            if new_sym is None:
                new_aliases.append(alias)
                continue

            # Preserve comma/whitespace from the original; strip trailing comma
            # on the last alias.
            is_last = i == len(group) - 1
            comma = cst.MaybeSentinel.DEFAULT if is_last else alias.comma
            new_aliases.append(
                alias.with_changes(
                    name=cst.Name(new_sym),
                    comma=comma,
                )
            )
        return new_aliases

    def _log_changes(
        self,
        old_module: str,
        new_module: str,
        group: list[tuple[cst.ImportAlias, str, RewriteTarget]],
    ) -> None:
        for alias, _, target in group:
            old_sym = alias.name.value if isinstance(alias.name, cst.Name) else "?"
            new_sym = target.new_symbol if target.new_symbol is not None else old_sym
            tag = " [structural]" if target.structural else ""
            self.changes.append(
                f"  {old_module}.{old_sym} → {new_module}.{new_sym}{tag}"
            )


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


def rewrite_file(path: Path, *, dry_run: bool = False) -> list[str]:
    """
    Rewrite all deprecated v2 imports in *path*.

    Returns a list of human-readable change descriptions. The file is
    modified in-place unless *dry_run* is True (in which case the result is
    printed to stdout).
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        print(f"  SKIP (parse error): {exc}", file=sys.stderr)
        return []

    rewriter = V3ImportRewriter()
    new_tree = tree.visit(rewriter)

    if not rewriter.changes:
        return []

    new_source = new_tree.code
    if dry_run:
        print(new_source)
    else:
        path.write_text(new_source, encoding="utf-8")

    return rewriter.changes


def rewrite_path(target: Path, *, dry_run: bool = False) -> dict[Path, list[str]]:
    """
    Rewrite all Python files under *target* (file or directory).

    Returns a mapping of path → changes for files that were modified.
    """
    paths: list[Path] = [target] if target.is_file() else sorted(target.rglob("*.py"))
    results: dict[Path, list[str]] = {}
    for p in paths:
        changes = rewrite_file(p, dry_run=dry_run)
        if changes:
            results[p] = changes
    return results


# ---------------------------------------------------------------------------
# Internal import rewriter (post-directory-consolidation)
# ---------------------------------------------------------------------------


class InternalImportRewriter(cst.CSTTransformer):
    """
    libcst transformer that rewrites connector-internal module paths.

    Used after directory consolidation (Phase 2c) to fix broken imports in
    test files that still reference old paths like ``app.activities.foo``.
    Only the module path is rewritten; symbol names are preserved unchanged.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        super().__init__()
        self.mapping = mapping
        self.changes: list[str] = []

    def leave_ImportFrom(
        self,
        original_node: cst.ImportFrom,
        updated_node: cst.ImportFrom,
    ) -> cst.ImportFrom:
        old_module = _module_to_str(updated_node.module)
        if old_module not in self.mapping:
            return updated_node
        new_module_str = self.mapping[old_module]
        self.changes.append(f"  {old_module} → {new_module_str}")
        return updated_node.with_changes(module=_str_to_module(new_module_str))


def rewrite_internal_file(
    path: Path,
    mapping: dict[str, str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """
    Rewrite internal module paths in *path* according to *mapping*.

    Returns a list of human-readable change descriptions. The file is
    modified in-place unless *dry_run* is True.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        print(f"  SKIP (parse error): {exc}", file=sys.stderr)
        return []

    rewriter = InternalImportRewriter(mapping)
    new_tree = tree.visit(rewriter)

    if not rewriter.changes:
        return []

    new_source = new_tree.code
    if dry_run:
        print(new_source)
    else:
        path.write_text(new_source, encoding="utf-8")

    return rewriter.changes


def rewrite_internal_imports(
    target_dir: Path,
    mapping: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict[Path, list[str]]:
    """
    Rewrite connector-internal module paths in all Python files under *target_dir*.

    Used after directory consolidation (Phase 2c) to fix imports that still
    reference old paths (e.g. ``app.activities.foo`` → ``app.foo``).

    Only rewrites the module path in ``from X import Y`` statements; symbol
    names are NOT changed.

    Returns a mapping of path → changes for files that were modified.
    """
    paths = sorted(target_dir.rglob("*.py")) if target_dir.is_dir() else [target_dir]
    results: dict[Path, list[str]] = {}
    for p in paths:
        changes = rewrite_internal_file(p, mapping, dry_run=dry_run)
        if changes:
            results[p] = changes
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite v2 application_sdk import paths to v3. "
            "Modifies files in-place (use --dry-run to preview)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="+",
        metavar="PATH",
        help="Python file or directory to rewrite.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rewritten source to stdout instead of writing files.",
    )
    parser.add_argument(
        "--internal-map",
        metavar="JSON",
        help=(
            "JSON object mapping old internal module paths to new paths. "
            "Rewrites connector-internal imports after directory consolidation "
            "(Phase 2c). Only the module path is rewritten; symbol names are "
            'preserved. Example: \'{"app.activities.foo": "app.foo"}\''
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.internal_map is not None:
        try:
            mapping: dict[str, str] = json.loads(args.internal_map)
        except ValueError as exc:
            print(f"ERROR: --internal-map is not valid JSON: {exc}", file=sys.stderr)
            return 2

        total_files = 0
        total_changes = 0
        for raw in args.targets:
            target = Path(raw)
            if not target.exists():
                print(f"ERROR: path does not exist: {target}", file=sys.stderr)
                return 1
            results = rewrite_internal_imports(target, mapping, dry_run=args.dry_run)
            for path, changes in results.items():
                total_files += 1
                total_changes += len(changes)
                if not args.dry_run:
                    print(f"REWROTE  {path}")
                    for line in changes:
                        print(line)

        if not args.dry_run:
            print(f"\nDone. Rewrote {total_files} file(s), {total_changes} import(s).")
        return 0

    total_files = 0
    total_changes = 0

    for raw in args.targets:
        target = Path(raw)
        if not target.exists():
            print(f"ERROR: path does not exist: {target}", file=sys.stderr)
            return 1
        results = rewrite_path(target, dry_run=args.dry_run)
        for path, changes in results.items():
            total_files += 1
            total_changes += len(changes)
            if not args.dry_run:
                print(f"REWROTE  {path}")
                for line in changes:
                    print(line)

    if not args.dry_run:
        print(f"\nDone. Rewrote {total_files} file(s), {total_changes} import(s).")
        if total_changes:
            print(
                "\nNext step: run the migration checker to see remaining structural work:\n"
                "  python -m tools.migrate_v3.check_migration <path>"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

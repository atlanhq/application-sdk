"""A8: Composable codemod pipeline CLI.

Chains all Phase 1 + Phase 2 codemods (A1–A7) across a connector directory,
applies them in dependency order, and writes the results back to disk.

Usage
-----
::

    # Auto-detect connector type and run all codemods
    python -m tools.migrate_v3.run_codemods ../atlan-anaplan-app/

    # Dry-run: print what would change without writing files
    python -m tools.migrate_v3.run_codemods --dry-run ../my-connector/

    # Override detected connector type
    python -m tools.migrate_v3.run_codemods --connector-type sql_metadata ../my-connector/

    # Skip specific codemods
    python -m tools.migrate_v3.run_codemods --skip A7 ../my-connector/

Programmatic API
----------------
::

    from tools.migrate_v3.run_codemods import run_codemods
    result = run_codemods(Path("../my-connector/"), dry_run=True)
    print(result.files_changed)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import libcst as cst

from tools.migrate_v3.check_migration import _is_test_path
from tools.migrate_v3.codemods import AddImportTransformer, BaseCodemod
from tools.migrate_v3.codemods.remove_activities_cls import RemoveActivitiesClsCodemod
from tools.migrate_v3.codemods.remove_decorators import RemoveDecoratorsCodemod
from tools.migrate_v3.codemods.rewrite_activity_calls import RewriteActivityCallsCodemod
from tools.migrate_v3.codemods.rewrite_entry_point import (
    RewriteEntryPointCodemod,
    derive_app_class_name,
)
from tools.migrate_v3.codemods.rewrite_handlers import RewriteHandlersCodemod
from tools.migrate_v3.codemods.rewrite_returns import RewriteReturnsCodemod
from tools.migrate_v3.codemods.rewrite_signatures import RewriteSignaturesCodemod
from tools.migrate_v3.fingerprint import fingerprint_connector

# Ordered codemod IDs — dependency order must be preserved.
_CODEMOD_ORDER = ["A1", "A3", "A4", "A5", "A2", "A6", "A7"]

# Directories to skip entirely.
_SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git", "node_modules", ".tox"})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    connector_type: str
    confidence: float
    files_changed: int = 0
    changes_by_file: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    type_errors: list[str] = field(default_factory=list)

    @property
    def todos_inserted(self) -> int:
        """Count SKIPPED entries — proxies for manual TODO items."""
        count = 0
        for file_changes in self.changes_by_file.values():
            for changes in file_changes.values():
                count += sum(1 for c in changes if c.startswith("SKIPPED"))
        return count

    def to_dict(self) -> dict:  # type: ignore[return]
        """Return a JSON-serialisable representation of the result."""
        d = asdict(self)
        d["todos_inserted"] = self.todos_inserted
        d["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
        return d

    def write_report(self, path: Path) -> None:
        """Write a ``migration_report.json`` to *path*."""
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# E3: Type-check gate
# ---------------------------------------------------------------------------


def _run_pyright(root: Path) -> list[str]:
    """Run pyright on *root* and return a list of error strings.

    Returns an empty list if pyright is not installed or reports no errors.
    Errors are returned as ``"<file>:<line>: <message>"`` strings.
    """
    try:
        result = subprocess.run(
            ["python", "-m", "pyright", "--outputjson", str(root)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return ["pyright not found — install with: uv add pyright --dev"]
    except subprocess.TimeoutExpired:
        return ["pyright timed out after 120 s"]

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Pyright may not support --outputjson; fall back to stderr text
        return [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "error:" in line.lower()
        ]

    errors: list[str] = []
    for diag in data.get("generalDiagnostics", []):
        if diag.get("severity") == "error":
            file_path = diag.get("file", "?")
            line = diag.get("range", {}).get("start", {}).get("line", 0)
            message = diag.get("message", "?")
            errors.append(f"{file_path}:{line + 1}: {message}")
    return errors


# ---------------------------------------------------------------------------
# App class name derivation
# ---------------------------------------------------------------------------


def _should_skip_dir(d: Path) -> bool:
    return d.name in _SKIP_DIRS or d.name.startswith(".")


def _collect_py_files(root: Path) -> list[Path]:
    """Collect all .py files under *root*, skipping ignored directories."""
    results: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(_should_skip_dir(p) for p in path.parents):
            continue
        results.append(path)
    return results


# ---------------------------------------------------------------------------
# Per-file pipeline
# ---------------------------------------------------------------------------


def _build_codemods(
    connector_type: str,
    app_class_name: str,
    skip: set[str],
) -> list[tuple[str, BaseCodemod]]:
    """Build the ordered list of (id, codemod) pairs for one file."""
    codemods: list[tuple[str, BaseCodemod]] = []
    if "A1" not in skip:
        codemods.append(("A1", RemoveDecoratorsCodemod(connector_type=connector_type)))
    if "A3" not in skip:
        codemods.append(("A3", RewriteSignaturesCodemod(connector_type=connector_type)))
    if "A4" not in skip:
        codemods.append(("A4", RewriteReturnsCodemod(connector_type=connector_type)))
    if "A5" not in skip:
        codemods.append(("A5", RewriteHandlersCodemod()))
    if "A2" not in skip:
        codemods.append(("A2", RewriteActivityCallsCodemod()))
    if "A6" not in skip:
        codemods.append(("A6", RemoveActivitiesClsCodemod()))
    if "A7" not in skip:
        codemods.append(("A7", RewriteEntryPointCodemod(app_class_name=app_class_name)))
    return codemods


def _run_file(
    path: Path,
    connector_type: str,
    app_class_name: str,
    skip: set[str],
) -> tuple[str | None, dict[str, list[str]]]:
    """Apply all codemods to one file.

    Returns ``(new_source, {codemod_id: [changes]})`` or ``(None, {})`` on error.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, {"error": [f"Read error: {exc}"]}

    try:
        tree = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return None, {"error": [f"Parse error: {exc}"]}

    all_changes: dict[str, list[str]] = {}
    all_imports_to_add: list[tuple[str, str]] = []

    for codemod_id, codemod in _build_codemods(connector_type, app_class_name, skip):
        tree = codemod.transform(tree)
        if codemod.changes:
            all_changes[codemod_id] = list(codemod.changes)
        all_imports_to_add.extend(codemod.imports_to_add)

    if all_imports_to_add:
        adder = AddImportTransformer(all_imports_to_add)
        tree = tree.visit(adder)

    new_source = tree.code
    return new_source, all_changes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_codemods(
    root: Path,
    connector_type: str | None = None,
    dry_run: bool = False,
    skip: list[str] | None = None,
    type_check: bool = False,
    output_json: Path | None = None,
) -> RunResult:
    """Run the full codemod pipeline on *root*.

    Parameters
    ----------
    root:
        Directory (or single file) to process.
    connector_type:
        Override the auto-detected connector type.  One of ``sql_metadata``,
        ``incremental_sql``, ``sql_query``, ``custom``.
    dry_run:
        If True, compute and return changes without writing any files.
    skip:
        List of codemod IDs to skip, e.g. ``["A7"]``.
    type_check:
        If True (and not dry_run), run pyright on *root* after applying codemods
        and record any type errors in :attr:`RunResult.type_errors`.
    output_json:
        If given, write a machine-readable report to this path after the run.

    Returns
    -------
    RunResult
    """
    skip_set = set(skip or [])
    root = root.resolve()

    # Fingerprint
    fp = fingerprint_connector(root)
    ct = connector_type or fp.connector_type
    app_class_name = derive_app_class_name(root)

    result = RunResult(connector_type=ct, confidence=fp.confidence)

    py_files = _collect_py_files(root) if root.is_dir() else [root]

    for path in py_files:
        # Skip test files for structural codemods (tests are handled by import rewriter only).
        if _is_test_path(path, root=root if root.is_dir() else root.parent):
            continue

        new_source, file_changes = _run_file(path, ct, app_class_name, skip_set)

        if "error" in file_changes:
            result.errors.append(f"{path}: {file_changes['error'][0]}")
            continue

        if not file_changes:
            continue  # Nothing changed.

        try:
            rel = str(path.relative_to(root.parent if root.is_file() else root))
        except ValueError:
            rel = path.name

        result.changes_by_file[rel] = file_changes
        result.files_changed += 1

        if not dry_run and new_source is not None:
            path.write_text(new_source, encoding="utf-8")

    # E3: optional type-check gate (only meaningful after files are written)
    if type_check and not dry_run:
        result.type_errors = _run_pyright(root if root.is_dir() else root.parent)

    # F1: optional JSON report
    if output_json is not None:
        result.write_report(output_json)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_v3.run_codemods",
        description="Run the v2→v3 codemod pipeline on a connector directory.",
    )
    parser.add_argument("path", help="Path to connector directory or Python file")
    parser.add_argument(
        "--connector-type",
        choices=["sql_metadata", "incremental_sql", "sql_query", "custom"],
        default=None,
        help="Override auto-detected connector type",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing files",
    )
    parser.add_argument(
        "--skip",
        metavar="ID[,ID...]",
        default="",
        help=f"Comma-separated codemod IDs to skip (choices: {', '.join(_CODEMOD_ORDER)})",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.add_argument(
        "--type-check",
        action="store_true",
        help="Run pyright on the target after applying codemods (requires pyright)",
    )
    parser.add_argument(
        "--output-json",
        metavar="PATH",
        default=None,
        help="Write a machine-readable migration_report.json to this path",
    )
    return parser


def _color(text: str, code: str, *, no_color: bool) -> str:
    if no_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    no_color = args.no_color

    root = Path(args.path)
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    skip = [s.strip() for s in args.skip.split(",") if s.strip()]

    output_json = Path(args.output_json) if args.output_json else None

    result = run_codemods(
        root,
        connector_type=args.connector_type,
        dry_run=args.dry_run,
        skip=skip,
        type_check=args.type_check,
        output_json=output_json,
    )

    # Header
    dry_tag = " (dry-run)" if args.dry_run else ""
    print(
        f"Fingerprint: {result.connector_type}"
        f" (confidence={result.confidence:.1f}){dry_tag}"
    )
    if not result.changes_by_file and not result.errors:
        print("No changes.")
        return 0

    print()

    for file_path, file_changes in result.changes_by_file.items():
        print(f"  {_color(file_path, '1', no_color=no_color)}")
        for codemod_id, changes in file_changes.items():
            for change in changes:
                prefix = "SKIPPED" if change.startswith("SKIPPED") else codemod_id
                color_code = "33" if change.startswith("SKIPPED") else "32"
                line = f"    {_color(prefix, color_code, no_color=no_color)}: {change}"
                print(line)
        print()

    for err in result.errors:
        print(f"  {_color('ERROR', '31', no_color=no_color)}: {err}", file=sys.stderr)

    if result.type_errors:
        print()
        for te in result.type_errors:
            print(
                f"  {_color('TYPE ERROR', '35', no_color=no_color)}: {te}",
                file=sys.stderr,
            )

    if output_json is not None:
        print(f"\nReport written to: {output_json}")

    # Summary
    todo_str = (
        f", {result.todos_inserted} TODOs inserted" if result.todos_inserted else ""
    )
    err_str = f", {len(result.errors)} errors" if result.errors else ""
    type_err_str = (
        f", {len(result.type_errors)} type error(s)" if result.type_errors else ""
    )
    print(
        f"Summary: {result.files_changed} file(s) changed"
        f"{todo_str}{err_str}{type_err_str}"
    )

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())

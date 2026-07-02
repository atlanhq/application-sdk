"""Generate the entrypoint-contract ledger from app/SDK source.

Usage
-----
Regenerate the committed ledger (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-contract-ledger

Check whether the committed ledger is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-contract-ledger --check

Scan a specific repo (defaults to auto-detected repo root):

    uv run atlan-application-sdk-conformance gen-contract-ledger --repo DIR

Design
------
Scans all Python files in *repo* for entrypoint contracts (same P013/P014
boundary logic as the B005/B006 checker uses) and builds or updates the
committed ledger.

**Append-only invariant**: the generator can add new entries and update the
``status`` of existing entries (active → deprecated → sunset), but it NEVER
deletes an entry or changes a recorded ``type``.  This means regenerating after
a removal does NOT launder the removal — B005 will still fire against the
persisted ledger entry.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover
from conformance.suite.checks.deprecation._contract_compat import (
    _collect_entrypoint_contract_names,
    _iter_fields,
)
from conformance.suite.checks.deprecation._ledger_schema import (
    ContractField,
    ContractLedger,
    load_ledger,
    serialize,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)


def build_ledger(repo_root: Path, existing: ContractLedger) -> ContractLedger:
    """Scan *repo_root* and build an updated ledger.

    Append-only: new fields are added; ``status`` is refreshed from source;
    ``type`` is NEVER changed; entries are NEVER deleted.
    """
    # Collect all Python files under the repo root
    paths = list(discover(repo_root))

    # Parse all files and build the class registry (needed for App-subclass
    # resolution during entrypoint detection)
    file_trees: dict[Path, ast.AST] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        file_trees[path] = tree
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    entrypoint_names = _collect_entrypoint_contract_names(file_trees, by_name)

    # Index existing ledger entries for fast lookup
    existing_by_key: dict[tuple[str, str], ContractField] = {
        (f.contract, f.field): f for f in existing.fields
    }

    # Collect all live contract fields from entrypoint classes
    live_entries: dict[tuple[str, str], ContractField] = {}
    for path, tree in file_trees.items():
        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            if class_node.name not in entrypoint_names:
                continue
            for fi in _iter_fields(class_node):
                key = (class_node.name, fi.name)
                live_entries[key] = ContractField(
                    contract=class_node.name,
                    field=fi.name,
                    type=fi.canonical_type,
                    status=fi.status,
                )

    # Merge: append-only with status refresh
    merged: dict[tuple[str, str], ContractField] = {}

    # Keep all existing entries — NEVER delete (append-only invariant)
    for key, existing_field in existing_by_key.items():
        live = live_entries.get(key)
        if live is not None:
            # Refresh status from source; type is frozen on first record
            merged[key] = ContractField(
                contract=existing_field.contract,
                field=existing_field.field,
                type=existing_field.type,  # NEVER change the recorded type
                status=live.status,
            )
        else:
            # Field removed from source — keep in ledger (B005 will flag it)
            merged[key] = existing_field

    # Add new live fields not yet in the ledger
    for key, live_field in live_entries.items():
        if key not in merged:
            merged[key] = live_field

    fields = sorted(merged.values(), key=lambda f: (f.contract, f.field))
    return ContractLedger(version=existing.version, fields=fields)


def _find_repo_root() -> Path | None:
    """Locate the repo root from cwd or this file's ancestors.

    A repo root is recognized by containing a ``pyproject.toml`` at the top
    level with Python source directories alongside.
    """
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for parent in [start, *start.parents]:
            if (parent / "pyproject.toml").is_file():
                return parent
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the entrypoint-contract ledger from source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repo root to scan (default: auto-detected from cwd).",
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=Path("contract_schema.lock.json"),
        help="Ledger path to write (default: contract_schema.lock.json in cwd).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed ledger matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    repo_root: Path | None = args.repo or _find_repo_root()
    if repo_root is None or not (repo_root / "pyproject.toml").is_file():
        print(
            "error: could not locate repo root (pyproject.toml) — pass --repo DIR.",
            file=sys.stderr,
        )
        sys.exit(2)

    outfile: Path = args.outfile
    existing = load_ledger(outfile if outfile.exists() else None)
    ledger = build_ledger(repo_root, existing)
    content = serialize(ledger)

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-contract-ledger` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Contract ledger up-to-date ({len(ledger.fields)} fields).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} ({len(ledger.fields)} fields).")


if __name__ == "__main__":
    main()

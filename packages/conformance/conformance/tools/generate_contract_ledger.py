"""Generate the entrypoint-contract ledger from app/SDK source.

Usage
-----
Regenerate the committed ledger in a **consumer app** (normal developer
workflow).  The version pin matters — see :func:`regen_command`: ``detect``
runs the checker from an unpinned ``uvx`` install, so a bare ``uv run`` here
would use the app's *locked* dev dependency and can produce a byte-identical
ledger that leaves a B006 finding standing (FND-607):

    uvx atlan-application-sdk-conformance==<version> gen-contract-ledger

In the SDK repo itself the suite is in-tree, so ``uv run`` is correct:

    uv run atlan-application-sdk-conformance gen-contract-ledger

There, from any directory in the checkout, it scans the whole SDK and writes the
repo-root ``contract_schema.lock.json`` — the SDK's only committed ledger, which
the conformance wheel's build hook packages (FND-3108). The ledger records the
template contracts' entrypoints and the SDK contract bases (``Input``,
``Output``, ``PublishInputMixin``) that every app contract inherits from.

Check whether the committed ledger is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-contract-ledger --check

Scan a specific repo (defaults to auto-detected repo root):

    uv run atlan-application-sdk-conformance gen-contract-ledger --repo DIR

Design
------
Scans all Python files in *repo* for entrypoint contracts (same P013/P014
boundary logic as the B005/B006 checker uses) and builds or updates the
committed ledger. Field extraction resolves the full base-class chain
(``resolve_contract_fields``): fields inherited from an in-repo base class or
from an SDK-provided mixin (e.g. ``PublishInputMixin``) are recorded exactly
like fields declared directly on the contract.

**Append-only invariant**: the generator can add new entries and update the
``status`` of existing entries (active → deprecated → sunset), but it NEVER
deletes an entry, changes a recorded ``type``, or moves an entry straight from
``active`` to ``sunset`` (it keeps ``active`` and B005 reports the skip).  This means regenerating after
a removal does NOT launder the removal — B005 will still fire against the
persisted ledger entry.

**Requiredness** (``required``: callers must send the field) is recorded the
same way.  It may relax from ``true`` to ``false`` but is never recorded moving
back, and a field added to a contract the ledger already records is recorded
``false`` whatever the source says.  A source that requires either field then
disagrees with the ledger, and B005 reports it: a caller that predates the field
does not send it.  A contract new to the ledger records its fields as declared.
An entry written before ``required`` existed has it unknown, and the first
regeneration records the source's current requiredness as its baseline.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import replace
from pathlib import Path

from conformance.suite.checks._ast_common import detect_scope, discover
from conformance.suite.checks._entrypoint_contract_fields import (
    _base_name,
    build_contract_registry,
    iter_entrypoint_contract_annotations,
    resolve_contract_fields,
    sdk_base_contract_names,
)
from conformance.suite.checks.deprecation._ledger_schema import (
    LEDGER_VERSION,
    ContractField,
    ContractLedger,
    load_ledger_baseline,
    regen_command,
    serialize,
)
from conformance.suite.checks.prescriptions._error_code_prefix import ClassRecord
from conformance.suite.schema.disposition import RuleScope

LEDGER_NAME = "contract_schema.lock.json"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class AmbiguousContractBase(ValueError):
    """An entrypoint contract's base is a name several modules bind differently.

    Recording it would mean guessing which module's class it is, and the ledger
    is append-only: a wrong guess stays recorded as that contract's fields.
    """


class DuplicateContractName(ValueError):
    """An entrypoint contract's class name is declared in more than one module.

    The ledger keys a contract by its bare class name, so it cannot keep the
    declarations apart: each one's fields would be recorded under the other.
    """


def _ambiguous_bases(
    class_node: ast.ClassDef,
    aliases: dict[str, str],
    by_name: dict[str, ClassRecord],
    ambiguous: frozenset[str],
) -> set[str]:
    """Names in *class_node*'s ancestry that resolve only through an ambiguous name.

    *by_name* is the registry without ambiguous bare names
    (:attr:`ContractRegistry.resolvable`), so an ambiguous name is never
    walked through a guessed declaration.
    """
    found: set[str] = set()
    seen: set[str] = {class_node.name}
    stack = [aliases.get(n, n) for b in class_node.bases if (n := _base_name(b))]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        rec = by_name.get(name)
        if rec is not None:
            stack.extend(rec.bases)
        elif name in ambiguous:
            found.add(name)
    return found


def build_ledger(repo_root: Path, existing: ContractLedger) -> ContractLedger:
    """Scan *repo_root* and build an updated ledger.

    Append-only: new fields are added; ``status`` is refreshed from source;
    ``type`` is NEVER changed; entries are NEVER deleted; ``required`` never
    moves from ``False`` to ``True``.

    Raises :class:`AmbiguousContractBase` instead of recording a contract whose
    base only resolves through a name several modules declare or rebind to
    different classes (import the class from its module, or by its own name),
    and :class:`DuplicateContractName` when an entrypoint contract's own class
    name is declared in more than one module.
    """
    # Collect all Python files under the repo root
    paths = list(discover(repo_root))

    # Parse all files and build the class registry (needed for App-subclass
    # resolution during entrypoint detection, and for inherited-field resolution)
    file_trees: dict[Path, ast.AST] = {}
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

    rels = {path: _rel(path, repo_root) for path in file_trees}
    registry = build_contract_registry(file_trees, rels)
    paths_by_rel = {rel: path for path, rel in rels.items()}

    # The declaration each entrypoint annotates, resolved through the
    # annotating file's imports: a same-named class elsewhere in the repo (a
    # generated base the contract subclasses under its own name, a stale copy,
    # an unrelated model) is not that entrypoint's contract.
    contracts: dict[int, ClassRecord] = {}
    for path, name in iter_entrypoint_contract_annotations(
        file_trees, registry.by_name
    ):
        target = registry.aliases[path].get(name, name)
        rec = registry.resolvable.get(target)
        if rec is not None:
            contracts[id(rec.node)] = rec
        elif target in registry.ambiguous:
            raise AmbiguousContractBase(
                f"{rels[path]}: an entrypoint is annotated {name!r}, which "
                "several modules declare or bind to different classes. Import "
                "it from the module that defines it; the ledger is not "
                "written rather than guessing."
            )
    for name in sdk_base_contract_names(registry.by_name):
        rec = registry.by_name[name]
        contracts[id(rec.node)] = rec

    by_contract: dict[str, list[ClassRecord]] = {}
    for rec in contracts.values():
        by_contract.setdefault(rec.name, []).append(rec)
    for name, recs in sorted(by_contract.items()):
        if len(recs) > 1:
            files = ", ".join(sorted(rec.file for rec in recs))
            raise DuplicateContractName(
                f"entrypoint contract {name!r} is declared more than once "
                f"({files}). The ledger records a contract under its class "
                "name, so it cannot tell these apart; rename all but one "
                "(regenerate with a current contract-toolkit, which prefixes "
                "each entrypoint's class). The ledger is not written rather "
                "than mixing their fields."
            )

    # Index existing ledger entries for fast lookup
    existing_by_key: dict[tuple[str, str], ContractField] = {
        (f.contract, f.field): f for f in existing.fields
    }

    # Collect all live contract fields from entrypoint classes
    live_entries: dict[tuple[str, str], ContractField] = {}
    for rec in sorted(contracts.values(), key=lambda r: (r.file, r.node.lineno)):
        aliases = registry.aliases.get(paths_by_rel[rec.file], {})
        unresolved = _ambiguous_bases(
            rec.node, aliases, registry.resolvable, registry.ambiguous
        )
        if unresolved:
            names = ", ".join(sorted(unresolved))
            raise AmbiguousContractBase(
                f"{rec.file}: contract {rec.name!r} "
                f"inherits {names}, which several modules declare or bind "
                "to different classes. Import the base from the module "
                "that defines it (`from app.generated.<entrypoint>._input "
                "import AppInputContract`) or by its own class name; the "
                "ledger is not written rather than guessing."
            )
        for fi in resolve_contract_fields(rec.node, aliases, registry.resolvable):
            key = (rec.name, fi.name)
            live_entries[key] = ContractField(
                contract=rec.name,
                field=fi.name,
                type=fi.canonical_type,
                status=fi.status,
                required=fi.required,
            )

    # Merge: append-only with status refresh
    merged: dict[tuple[str, str], ContractField] = {}

    # Keep all existing entries — NEVER delete (append-only invariant)
    for key, existing_field in existing_by_key.items():
        live = live_entries.get(key)
        if live is not None:
            # Refresh status from source; type is frozen on first record.
            # 'active' never moves straight to 'sunset': the ledger keeps
            # 'active' so B005 reports the skipped deprecation instead of the
            # regeneration recording it.
            status = live.status
            if existing_field.status == "active" and status == "sunset":
                status = existing_field.status
            merged[key] = ContractField(
                contract=existing_field.contract,
                field=existing_field.field,
                type=existing_field.type,  # NEVER change the recorded type
                status=status,
                required=_merged_required(existing_field.required, live.required),
            )
        else:
            # Field removed from source — keep in ledger (B005 will flag it)
            merged[key] = existing_field

    # Add new live fields not yet in the ledger. On a contract the ledger
    # already records, a new field is recorded not-required whatever the
    # source says: callers that predate it cannot send it, so B005 reports a
    # source that requires it instead of the regeneration recording it.
    recorded_contracts = {contract for contract, _ in existing_by_key}
    for key, live_field in live_entries.items():
        if key not in merged:
            if live_field.required and live_field.contract in recorded_contracts:
                live_field = replace(live_field, required=False)
            merged[key] = live_field

    fields = sorted(merged.values(), key=lambda f: (f.contract, f.field))
    return ContractLedger(version=max(existing.version, LEDGER_VERSION), fields=fields)


def _merged_required(recorded: bool | None, live: bool | None) -> bool | None:
    """The ``required`` to record over *recorded*, given the source's *live*.

    Unknown takes the source's value (the backfill); ``True`` follows the
    source, so a field may relax; ``False`` stays ``False``, so B005 reports a
    source that now requires it.  An unknown source keeps what is recorded.
    """
    if recorded is False or live is None:
        return recorded
    return live


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


def _sdk_checkout_root(start: Path) -> Path | None:
    """The SDK checkout containing *start*: the dir holding ``application_sdk/`` and ``packages/conformance/``."""
    for parent in [start, *start.parents]:
        if (parent / "application_sdk").is_dir() and (
            parent / "packages" / "conformance" / "pyproject.toml"
        ).is_file():
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
        default=None,
        help=(
            "Ledger path to write (default: contract_schema.lock.json in cwd; "
            "in the SDK repo, the repo-root contract_schema.lock.json)."
        ),
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

    scope = detect_scope(repo_root)
    if scope is RuleScope.SDK and args.repo is None:
        repo_root = _sdk_checkout_root(repo_root.resolve()) or repo_root
    outfile: Path = args.outfile or (
        repo_root / LEDGER_NAME if scope is RuleScope.SDK else Path(LEDGER_NAME)
    )
    # A first ledger starts EMPTY — see load_ledger_baseline for why, and note
    # that bootstrap's write-if-absent scaffold shares the same helper so the
    # invariant cannot drift between the two writers.
    try:
        ledger = build_ledger(repo_root, load_ledger_baseline(outfile))
    except (AmbiguousContractBase, DuplicateContractName) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    content = serialize(ledger)

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `{regen_command(scope)}` to update.",
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

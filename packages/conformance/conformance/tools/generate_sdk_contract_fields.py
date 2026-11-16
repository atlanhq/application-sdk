"""Generate the SDK contract-field tables that ``_sdk_contract_mixins`` loads.

Usage
-----
Regenerate the committed table (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-sdk-contract-fields

Check whether the committed table is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-sdk-contract-fields --check

Design
------
SDK-provided contract classes are absent from a consumer app's AST scan, so
the checks that resolve contract fields (B005/B006, K016, K018, the ledger
generator, preflight) read them from a table baked into the wheel instead.
This tool builds that table from the importable ``application_sdk`` package —
in this repo, the ``test`` extra resolves it editable from the checkout (see
``[tool.uv.sources]`` in ``packages/conformance/pyproject.toml``) — and writes
``conformance/data/sdk_contract_fields.json``.

Four tables are emitted, matching the four public names in
``_sdk_contract_mixins``:

- ``contract_base_fields`` — own fields of the contract roots in
  ``application_sdk.contracts.base`` (``Input`` / ``Output`` /
  ``PublishInputMixin``), read with the checker's field extractor.
- ``template_contract_fields`` — every contract class under
  ``application_sdk.templates.contracts``, flattened across its whole base
  chain by running ``resolve_contract_fields`` itself over the live source, so
  each entry is by construction what the checker would compute. Bare names
  another SDK contract module also declares are left out: the registry is keyed
  by bare name and cannot tell them apart.
- ``template_contract_bases`` — each template contract's registered ancestors.
- ``template_run_contracts`` — each template whose ``run()`` is not
  ``App.run``, with the input and return type names ``get_type_hints`` reports,
  which is how ``_collect_implicit_ep`` resolves an inherited ``run()``.

The AST tables read source files of the same installed package the run-contract
table imports, so all four come from one SDK version.

The resolver this tool reuses imports ``_sdk_contract_mixins``, which refuses to
load a missing or broken table. That module is imported here with its bootstrap
switch set, so the tool can still recreate the table. The registries it then
holds are never consulted: every SDK class is found in the SDK source being
scanned, and the registries are only the fallback for classes that are not.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.resources as _ir
import importlib.util
import json
import os
import sys
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)

SDK_PACKAGE = "application_sdk"

#: Must equal ``_sdk_contract_mixins.DATA_PATH``. It is not imported from there,
#: because importing that module loads (and validates) the very file this tool
#: may need to recreate.
DATA_PATH = Path(str(_ir.files("conformance"))).joinpath(
    "data", "sdk_contract_fields.json"
)

#: Must equal ``_sdk_contract_mixins.BOOTSTRAP_ENV``, for the same reason.
BOOTSTRAP_ENV = "_CONFORMANCE_SDK_CONTRACT_FIELDS_BOOTSTRAP"

#: Root contract classes: anything transitively derived from one of these is a
#: contract, and each has an entry in ``contract_base_fields``.
CONTRACT_ROOTS: tuple[str, ...] = ("Input", "Output", "PublishInputMixin")
_CONTRACT_BASE_MODULE = "application_sdk.contracts.base"
TEMPLATE_PKG = "application_sdk.templates.contracts"


@contextlib.contextmanager
def _bootstrap_env() -> Iterator[None]:
    previous = os.environ.get(BOOTSTRAP_ENV)
    os.environ[BOOTSTRAP_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(BOOTSTRAP_ENV, None)
        else:
            os.environ[BOOTSTRAP_ENV] = previous


class SdkSources(NamedTuple):
    """Every parsed SDK module, plus the indexes derived from all of them."""

    per_module: dict[str, tuple[dict[str, str], list[ClassRecord]]]
    by_name: dict[str, ClassRecord]
    contract_owners: dict[str, set[str]]


def find_sdk_package() -> Path | None:
    """Directory of the importable ``application_sdk`` package, if any."""
    spec = importlib.util.find_spec(SDK_PACKAGE)
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).parent


def parse_sdk_sources(sdk_pkg: Path) -> SdkSources:
    """Parse every module in the SDK and index its contract classes.

    The whole package is scanned rather than just the template modules for two
    reasons: ancestor resolution has to reach ``contracts.base`` (and any other
    module a template contract inherits through), and the bare-name collision
    set can only be computed against every contract class the SDK declares.
    """
    per_module: dict[str, tuple[dict[str, str], list[ClassRecord]]] = {}
    for path in sorted(sdk_pkg.rglob("*.py")):
        parts = [
            p
            for p in path.relative_to(sdk_pkg).with_suffix("").parts
            if p != "__init__"
        ]
        module = ".".join([SDK_PACKAGE, *parts])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = collect_import_aliases(tree)
        per_module[module] = (aliases, collect_classes(tree, module, aliases))

    by_name: dict[str, ClassRecord] = {}
    for _aliases, records in per_module.values():
        for record in records:
            by_name.setdefault(record.name, record)

    contract_memo: dict[str, bool] = {}

    def is_contract(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in CONTRACT_ROOTS:
            return True
        if name in contract_memo:
            return contract_memo[name]
        if name in seen:
            return False  # cycle — mirrors resolve_contract_fields
        record = by_name.get(name)
        result = record is not None and any(
            is_contract(base, seen | {name}) for base in record.bases
        )
        contract_memo[name] = result
        return result

    contract_owners: dict[str, set[str]] = {}
    for module, (_aliases, records) in per_module.items():
        for record in records:
            if is_contract(record.name):
                contract_owners.setdefault(record.name, set()).add(module)

    return SdkSources(per_module, by_name, contract_owners)


def ambiguous_template_names(sources: SdkSources) -> dict[str, set[str]]:
    """Template contract names that another SDK contract module also declares."""
    return {
        name: owners
        for name, owners in sources.contract_owners.items()
        if len(owners) > 1 and any(owner.startswith(TEMPLATE_PKG) for owner in owners)
    }


def _build_base_fields(sources: SdkSources) -> dict[str, list[list[str]]]:
    with _bootstrap_env():
        from conformance.suite.checks._entrypoint_contract_fields import _iter_fields
    _aliases, records = sources.per_module[_CONTRACT_BASE_MODULE]
    by_name = {record.name: record for record in records}
    table: dict[str, list[list[str]]] = {}
    for name in CONTRACT_ROOTS:
        record = by_name.get(name)
        if record is None:
            raise ValueError(f"{_CONTRACT_BASE_MODULE}.{name} not found")
        table[name] = [
            [fi.name, fi.canonical_type, fi.status] for fi in _iter_fields(record.node)
        ]
    return table


def _build_template_fields(sources: SdkSources) -> dict[str, list[list[str]]]:
    with _bootstrap_env():
        from conformance.suite.checks._entrypoint_contract_fields import (
            resolve_contract_fields,
        )

    flattened: dict[str, list[list[str]]] = {}
    for module, (aliases, records) in sources.per_module.items():
        if not module.startswith(TEMPLATE_PKG):
            continue
        for record in records:
            owners = sources.contract_owners.get(record.name)
            if not owners or len(owners) > 1:
                continue  # not a contract, or an ambiguous bare name
            fields = resolve_contract_fields(record.node, aliases, sources.by_name)
            flattened[record.name] = sorted(
                [fi.name, fi.canonical_type, fi.status] for fi in fields
            )
    return dict(sorted(flattened.items()))


def _build_template_bases(
    sources: SdkSources, known: set[str], templates: list[str]
) -> dict[str, list[str]]:
    def ancestors(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        record = sources.by_name.get(name)
        if record is None or name in seen:
            return set()
        found: set[str] = set()
        for base in record.bases:
            if base in known:
                found.add(base)
            found |= ancestors(base, seen | {name})
        return found

    return {name: sorted(ancestors(name)) for name in templates}


def _build_run_contracts() -> dict[str, dict[str, str]]:
    import application_sdk.templates as templates
    from application_sdk.app.base import App

    table: dict[str, dict[str, str]] = {}
    for name in templates.__all__:
        run = getattr(templates, name).run
        if run is App.run:
            continue  # no implicit entrypoint for a subclass without run()
        hints = typing.get_type_hints(run)
        table[name] = {
            "input": hints["input"].__name__,
            "output": hints["return"].__name__,
        }
    return table


def build_sdk_contract_fields(sdk_pkg: Path) -> dict[str, object]:
    """Build all four tables from the SDK package at *sdk_pkg*."""
    sources = parse_sdk_sources(sdk_pkg)
    base_fields = _build_base_fields(sources)
    template_fields = _build_template_fields(sources)
    known = set(base_fields) | set(template_fields)
    return {
        "contract_base_fields": base_fields,
        "template_contract_fields": template_fields,
        "template_contract_bases": _build_template_bases(
            sources, known, list(template_fields)
        ),
        "template_run_contracts": _build_run_contracts(),
    }


def serialize(table: dict[str, object]) -> str:
    """Deterministic JSON so ``--check`` is a stable staleness gate."""
    return json.dumps(table, indent=2) + "\n"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the SDK contract-field tables from application_sdk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=DATA_PATH,
        help=f"Table path to write (default: {DATA_PATH}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed table matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    sdk_pkg = find_sdk_package()
    if sdk_pkg is None:
        print(
            f"error: {SDK_PACKAGE} is not importable — install this package's "
            "`test` extra (`uv sync --all-extras`).",
            file=sys.stderr,
        )
        sys.exit(2)

    outfile: Path = args.outfile
    if args.check and not outfile.exists():
        print(f"MISSING: {outfile}", file=sys.stderr)
        sys.exit(1)

    content = serialize(build_sdk_contract_fields(sdk_pkg))

    if args.check:
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-sdk-contract-fields` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("SDK contract-field table up-to-date.")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile}.")


if __name__ == "__main__":
    main()

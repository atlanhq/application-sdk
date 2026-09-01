"""Sync guard for the static SDK contract-base field registry.

``_sdk_contract_mixins.SDK_CONTRACT_BASE_FIELDS`` hand-mirrors the fields of
``application_sdk.contracts.base.Input`` / ``Output`` / ``PublishInputMixin``,
because those classes live outside the scanned repo when a checker (or the
ledger generator) runs against a consumer app — see ``resolve_contract_fields``
in the neutral ``_entrypoint_contract_fields`` module.

These tests keep both registries honest as the SDK evolves: they locate the
real ``application_sdk`` package (a ``test`` extra dependency of this package),
AST-parse its sources, and rebuild each registry from them.

``SDK_CONTRACT_BASE_FIELDS`` is rebuilt with the same field extractor the
checker uses, per class, own fields only. ``SDK_TEMPLATE_CONTRACT_FIELDS`` is
rebuilt by running ``resolve_contract_fields`` itself over the live template
modules, so the expected value is by construction what the checker would
compute for that class — and it is checked for completeness and for the
bare-name collisions the registry deliberately excludes, not only for drift in
the classes already listed.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
from pathlib import Path
from typing import NamedTuple

import pytest
from conformance.suite.checks._entrypoint_contract_fields import (
    _iter_fields,
    resolve_contract_fields,
)
from conformance.suite.checks._sdk_contract_mixins import (
    SDK_CONTRACT_BASE_FIELDS,
    SDK_TEMPLATE_CONTRACT_FIELDS,
    SdkField,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)

_sdk_spec = importlib.util.find_spec("application_sdk.contracts.base")
_sdk_pkg_spec = importlib.util.find_spec("application_sdk")

# Root contract classes: anything transitively derived from one of these is a
# contract, and every one of them is registered in SDK_CONTRACT_BASE_FIELDS.
_CONTRACT_ROOTS = frozenset({"Input", "Output", "PublishInputMixin"})
_TEMPLATE_PKG = "application_sdk.templates.contracts"

_requires_sdk = pytest.mark.skipif(
    _sdk_pkg_spec is None or _sdk_pkg_spec.origin is None,
    reason="atlan-application-sdk (test extra) is not installed",
)


@pytest.mark.skipif(
    _sdk_spec is None or _sdk_spec.origin is None,
    reason="atlan-application-sdk (test extra) is not installed",
)
def test_registry_matches_live_sdk_source() -> None:
    assert _sdk_spec is not None and _sdk_spec.origin is not None  # narrow for mypy
    source = ast.parse(Path(_sdk_spec.origin).read_text(encoding="utf-8"))

    by_name: dict[str, ast.ClassDef] = {
        node.name: node for node in ast.walk(source) if isinstance(node, ast.ClassDef)
    }

    for class_name, expected_fields in SDK_CONTRACT_BASE_FIELDS.items():
        classdef = by_name.get(class_name)
        assert classdef is not None, (
            f"application_sdk.contracts.base.{class_name} not found — "
            "update _sdk_contract_mixins.SDK_CONTRACT_BASE_FIELDS"
        )
        live = {
            fi.name: (fi.canonical_type, fi.status) for fi in _iter_fields(classdef)
        }
        expected = {sf.name: (sf.canonical_type, sf.status) for sf in expected_fields}
        assert live == expected, (
            f"application_sdk.contracts.base.{class_name} drifted from the static "
            "registry in _sdk_contract_mixins.py — update SDK_CONTRACT_BASE_FIELDS "
            f"to match. live={live} registry={expected}"
        )


def test_registry_fields_are_well_formed() -> None:
    """Every registry entry is a valid SdkField with a non-empty name/type."""
    for class_name, entries in SDK_CONTRACT_BASE_FIELDS.items():
        assert entries, f"{class_name} has an empty field list"
        for entry in entries:
            assert isinstance(entry, SdkField)
            assert entry.name
            assert entry.canonical_type
            assert entry.status in ("active", "deprecated", "sunset")


# ── SDK template contract registry ────────────────────────────────────────────


class _SdkSources(NamedTuple):
    """Every parsed SDK module, plus the indexes derived from all of them."""

    per_module: dict[str, tuple[dict[str, str], list[ClassRecord]]]
    by_name: dict[str, ClassRecord]
    contract_owners: dict[str, set[str]]


@functools.cache
def _sdk_sources() -> _SdkSources:
    """Parse every module in the installed SDK and index its contract classes.

    The whole package is scanned rather than just the template modules for two
    reasons: ancestor resolution has to reach ``contracts.base`` (and any other
    module a template contract inherits through), and the bare-name collision
    set can only be computed against every contract class the SDK declares.
    """
    assert _sdk_pkg_spec is not None and _sdk_pkg_spec.origin is not None
    root = Path(_sdk_pkg_spec.origin).parent

    per_module: dict[str, tuple[dict[str, str], list[ClassRecord]]] = {}
    for path in sorted(root.rglob("*.py")):
        parts = [
            p for p in path.relative_to(root).with_suffix("").parts if p != "__init__"
        ]
        module = ".".join(["application_sdk", *parts])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = collect_import_aliases(tree)
        per_module[module] = (aliases, collect_classes(tree, module, aliases))

    by_name: dict[str, ClassRecord] = {}
    for _module, (_aliases, records) in per_module.items():
        for record in records:
            by_name.setdefault(record.name, record)

    @functools.cache
    def is_contract(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in _CONTRACT_ROOTS:
            return True
        if name in seen:
            return False  # cycle — mirrors resolve_contract_fields
        record = by_name.get(name)
        if record is None:
            return False
        return any(is_contract(base, seen | {name}) for base in record.bases)

    contract_owners: dict[str, set[str]] = {}
    for module, (_aliases, records) in per_module.items():
        for record in records:
            if is_contract(record.name):
                contract_owners.setdefault(record.name, set()).add(module)

    return _SdkSources(per_module, by_name, contract_owners)


def _live_template_contracts() -> dict[str, dict[str, tuple[str, str]]]:
    """Flatten every unambiguous live template contract, keyed by class name.

    Uses ``resolve_contract_fields`` — the checker's own resolver — against a
    class registry built from live SDK source, so the expected field set is by
    construction the one the checker would compute. Names another SDK contract
    module also declares are omitted, matching the registry's exclusion.
    """
    sources = _sdk_sources()
    flattened: dict[str, dict[str, tuple[str, str]]] = {}

    for module, (aliases, records) in sources.per_module.items():
        if not module.startswith(_TEMPLATE_PKG):
            continue
        for record in records:
            owners = sources.contract_owners.get(record.name)
            if not owners or len(owners) > 1:
                continue  # not a contract, or an ambiguous bare name
            fields = resolve_contract_fields(record.node, aliases, sources.by_name)
            flattened[record.name] = {
                fi.name: (fi.canonical_type, fi.status) for fi in fields
            }

    return flattened


def _live_ambiguous_template_names() -> dict[str, set[str]]:
    """Template contract names that another SDK contract module also declares."""
    sources = _sdk_sources()
    return {
        name: owners
        for name, owners in sources.contract_owners.items()
        if len(owners) > 1 and any(owner.startswith(_TEMPLATE_PKG) for owner in owners)
    }


@_requires_sdk
def test_template_registry_matches_live_sdk_source() -> None:
    """Each registered template contract still flattens to its recorded fields."""
    live = _live_template_contracts()

    for class_name, expected_fields in SDK_TEMPLATE_CONTRACT_FIELDS.items():
        assert class_name in live, (
            f"{class_name} is registered in SDK_TEMPLATE_CONTRACT_FIELDS but is no "
            "longer an unambiguous contract class under "
            f"{_TEMPLATE_PKG} — remove the entry or update the registry"
        )
        expected = {sf.name: (sf.canonical_type, sf.status) for sf in expected_fields}
        assert live[class_name] == expected, (
            f"{_TEMPLATE_PKG}.{class_name} drifted from the static registry in "
            "_sdk_contract_mixins.py — update SDK_TEMPLATE_CONTRACT_FIELDS to "
            f"match. added/changed={ {k: v for k, v in live[class_name].items() if expected.get(k) != v} } "
            f"removed={sorted(set(expected) - set(live[class_name]))}"
        )


@_requires_sdk
def test_template_registry_is_complete() -> None:
    """Every unambiguous live template contract is registered.

    A template contract the registry does not list resolves to no inherited
    fields at all, which is the B005/B006 false positive this registry exists
    to prevent — so a newly added template contract has to fail here rather
    than silently produce findings against consumer apps.
    """
    missing = sorted(
        set(_live_template_contracts()) - set(SDK_TEMPLATE_CONTRACT_FIELDS)
    )
    assert not missing, (
        f"{len(missing)} template contract(s) under {_TEMPLATE_PKG} are not in "
        f"SDK_TEMPLATE_CONTRACT_FIELDS: {missing}. Add a flattened entry for "
        "each (own fields plus every inherited field) — the resolver looks up "
        "the immediate base name only and does not recurse."
    )


@_requires_sdk
def test_template_registry_excludes_only_ambiguous_bare_names() -> None:
    """The excluded set is exactly the bare names the SDK declares twice.

    The registry is keyed by bare class name, so a name two SDK contract
    modules declare cannot be resolved to one of them; those are left out on
    purpose. Pinning the set here means a collision the SDK later removes
    surfaces as a failing test (register the name) rather than as a permanent
    silent gap.
    """
    ambiguous = _live_ambiguous_template_names()
    assert sorted(ambiguous) == ["UploadInput", "UploadOutput"], (
        "the set of ambiguous template contract bare names changed — "
        f"now {({k: sorted(v) for k, v in ambiguous.items()})}. Register any name "
        "that is no longer ambiguous, and extend the exclusion note in "
        "_sdk_contract_mixins.py for any new one."
    )
    for name in ambiguous:
        assert name not in SDK_TEMPLATE_CONTRACT_FIELDS, (
            f"{name} is declared by more than one SDK contract module "
            f"({sorted(ambiguous[name])}) — a bare-name-keyed registry cannot "
            "tell them apart, so it must not be registered"
        )


def test_template_registry_fields_are_well_formed() -> None:
    """Every template registry entry is a valid, non-empty SdkField tuple."""
    for class_name, entries in SDK_TEMPLATE_CONTRACT_FIELDS.items():
        assert entries, f"{class_name} has an empty field list"
        for entry in entries:
            assert isinstance(entry, SdkField)
            assert entry.name
            assert entry.canonical_type
            assert entry.status in ("active", "deprecated", "sunset")

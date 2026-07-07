"""Sync guard for the static SDK contract-base field registry.

``_sdk_contract_mixins.SDK_CONTRACT_BASE_FIELDS`` hand-mirrors the fields of
``application_sdk.contracts.base.Input`` / ``Output`` / ``PublishInputMixin``,
because those classes live outside the scanned repo when a checker (or the
ledger generator) runs against a consumer app — see ``resolve_contract_fields``
in the neutral ``_contract_fields`` module.

This test keeps the registry honest as the SDK evolves: it locates the real
``application_sdk`` package (a ``test`` extra dependency of this package),
AST-parses its contract base module, extracts each registered class's own
fields with the same extractor the checker uses, and asserts the result
matches the static registry exactly. If the SDK adds/removes/retypes a field
on one of these classes, this test fails until the registry is updated.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from conformance.suite.checks._contract_fields import _iter_fields
from conformance.suite.checks._sdk_contract_mixins import (
    SDK_CONTRACT_BASE_FIELDS,
    SdkField,
)

_sdk_spec = importlib.util.find_spec("application_sdk.contracts.base")


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

"""Unit tests for the shared module-level rebinding resolver (``_ast_common``).

The resolver exists because a pkl-generated contract reaches a check under a
domain name (``OpenAPIConnectorInput = AppInputContract``) rather than as a
``ClassDef`` of its own, and a check that resolves classes by bare name then
reads the class as absent from a scan it is sitting in (FND-2605).

These tests pin the two properties that keep it safe to key a registry on:
it never invents a resolution, and it never displaces a real declaration.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    collect_module_alias_targets,
    register_alias_records,
)


def _targets(src: str, imports: dict[str, str] | None = None) -> dict[str, str]:
    return collect_module_alias_targets(ast.parse(src), imports)


def test_plain_and_typealias_rebindings_are_collected() -> None:
    assert _targets(
        "from typing import TypeAlias\nA = Base\nB: TypeAlias = Base\n"
    ) == {"A": "Base", "B": "Base"}


def test_attribute_rebinding_takes_the_leaf_name() -> None:
    assert _targets("A = contracts.Base\n") == {"A": "Base"}


def test_right_hand_side_is_dealiased_through_the_file_imports() -> None:
    assert _targets("A = _Base\n", {"_Base": "Base"}) == {"A": "Base"}


def test_subscripted_and_called_values_are_not_rebindings() -> None:
    """``list[Base]`` and ``Base()`` name a different thing, not another name for it."""
    assert _targets("A = list[Base]\nB = Base()\nC = 3\n") == {}


def test_self_assignment_is_skipped() -> None:
    """``Base = Base`` (the re-export idiom) names no other class."""
    assert _targets("Base = Base\n") == {}


def test_nested_rebindings_are_not_module_level() -> None:
    assert _targets("def f():\n    A = Base\n\nclass C:\n    D = Base\n") == {}


def test_first_binding_of_a_name_wins() -> None:
    assert _targets("A = First\nA = Second\n") == {"A": "First"}


def test_registration_follows_a_chain_to_the_record() -> None:
    by_name = {"Base": "record"}
    register_alias_records(by_name, {"Mid": "Base", "Public": "Mid"})
    assert by_name == {"Base": "record", "Mid": "record", "Public": "record"}


def test_registration_leaves_an_unresolvable_chain_alone() -> None:
    """A chain that never lands on a record stays absent, so the name still reports."""
    by_name = {"Base": "record"}
    register_alias_records(by_name, {"Orphan": "Missing"})
    assert by_name == {"Base": "record"}


def test_registration_terminates_on_a_cycle() -> None:
    by_name: dict[str, str] = {}
    register_alias_records(by_name, {"Left": "Right", "Right": "Left"})
    assert by_name == {}


def test_registration_never_shadows_an_existing_record() -> None:
    by_name = {"Base": "base-record", "Shadow": "shadow-record"}
    register_alias_records(by_name, {"Shadow": "Base"})
    assert by_name["Shadow"] == "shadow-record"

"""Tests for ``application_sdk.contracts.compat``.

``canonical_type_str`` decides what counts as a type change, so it is tested
directly; ``check_backwards_compatible`` is tested on the cases where the old
``base.is_backwards_compatible`` and this checker disagreed (FND-4685).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union

import pytest
from pydantic import Field

from application_sdk.contracts.base import ContractDeclarationError, Input, MaxItems
from application_sdk.contracts.compat import (
    CompatibilityError,
    Incompatibility,
    assert_backwards_compatible,
    canonical_type_str,
    check_backwards_compatible,
)

#: Two distinct enums that both render as ``Status`` — as ``old.Status`` and
#: ``new.Status`` would after a module move that also changed the values.
OldStatus = Enum("Status", {"A": "a"}, module="old")
NewStatus = Enum("Status", {"B": "b"}, module="new")


# =============================================================================
# canonical_type_str
# =============================================================================


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (int, "int"),
        (type(None), "None"),
        (Any, "Any"),
        (Optional[str], "str | None"),
        (str | None, "str | None"),
        (None | str, "str | None"),
        (Union[int, str], "int | str"),
        (Union[str, int], "int | str"),
        (str | int, "int | str"),
        (Union[str, int, None], "int | str | None"),
        (Optional[Union[str, int]], "int | str | None"),
        (List[int], "list[int]"),
        (list[int], "list[int]"),
        (Dict[str, Any], "dict[str, Any]"),
        (dict[str, str | int | None], "dict[str, int | str | None]"),
        (Tuple[int, ...], "tuple[int, ...]"),
        (Annotated[List[int], MaxItems(3)], "list[int]"),
        (Annotated[Optional[int], "meta"], "int | None"),
        (Literal["a", "b"], "Literal['a', 'b']"),
    ],
)
def test_canonical_type_str(annotation: object, expected: str) -> None:
    assert canonical_type_str(annotation) == expected


def test_union_arm_order_does_not_reach_the_canonical_form() -> None:
    assert canonical_type_str(Union[int, str, bytes]) == canonical_type_str(
        Union[bytes, str, int]
    )


# =============================================================================
# check_backwards_compatible
# =============================================================================


class TestCheckBackwardsCompatible:
    def test_union_reorder_is_compatible(self) -> None:
        class V1(Input):
            value: Union[int, str] = 0

        class V2(Input):
            value: Union[str, int] = 0

        assert check_backwards_compatible(V1, V2) == (True, [])

    def test_annotated_typing_alias_to_builtin_is_compatible(self) -> None:
        class V1(Input):
            ids: Annotated[List[int], MaxItems(3)] = Field(default_factory=list)

        class V2(Input):
            ids: Annotated[list[int], MaxItems(5)] = Field(default_factory=list)

        assert check_backwards_compatible(V1, V2) == (True, [])

    def test_optional_spellings_are_compatible(self) -> None:
        class V1(Input):
            name: Optional[str] = None

        class V2(Input):
            name: str | None = None

        assert check_backwards_compatible(V1, V2) == (True, [])

    def test_changed_arm_is_incompatible(self) -> None:
        class V1(Input):
            value: Union[int, str] = 0

        class V2(Input):
            value: Union[int, float] = 0

        ok, issues = check_backwards_compatible(V1, V2)
        assert ok is False
        assert issues == [
            Incompatibility(
                field="value",
                reason="type changed from 'int | str' to 'float | int'",
            )
        ]

    def test_removed_field_is_incompatible(self) -> None:
        class V1(Input):
            name: str = ""
            old: str = ""

        class V2(Input):
            name: str = ""

        ok, issues = check_backwards_compatible(V1, V2)
        assert ok is False
        assert issues == [Incompatibility(field="old", reason="field was removed")]

    def test_new_required_field_is_incompatible(self) -> None:
        class V1(Input):
            name: str = ""

        class V2(Input):
            name: str = ""
            added: int

        ok, issues = check_backwards_compatible(V1, V2)
        assert ok is False
        assert [i.field for i in issues] == ["added"]

    def test_new_field_with_default_is_compatible(self) -> None:
        class V1(Input):
            name: str = ""

        class V2(Input):
            name: str = ""
            added: int = 0

        assert check_backwards_compatible(V1, V2) == (True, [])

    def test_same_named_distinct_class_is_incompatible(self) -> None:
        # Both render as 'Status'; the values a payload may carry differ.
        class V1(Input):
            status: OldStatus | None = None

        class V2(Input):
            status: NewStatus | None = None

        ok, issues = check_backwards_compatible(V1, V2)
        assert ok is False
        assert [i.field for i in issues] == ["status"]
        assert "different class" in issues[0].reason

    def test_same_class_in_reordered_union_is_compatible(self) -> None:
        class V1(Input):
            status: OldStatus | int | None = None

        class V2(Input):
            status: int | OldStatus | None = None

        assert check_backwards_compatible(V1, V2) == (True, [])

    def test_non_contract_raises(self) -> None:
        class NotAContract:
            pass

        class V1(Input):
            name: str = ""

        with pytest.raises(ContractDeclarationError):
            check_backwards_compatible(NotAContract, V1)


def test_assert_backwards_compatible_raises_with_issues() -> None:
    class V1(Input):
        value: int = 0

    class V2(Input):
        value: str = ""

    with pytest.raises(CompatibilityError) as exc_info:
        assert_backwards_compatible(V1, V2)
    assert [i.field for i in exc_info.value.issues] == ["value"]


def test_assert_backwards_compatible_passes_on_reorder() -> None:
    class V1(Input):
        value: Union[int, str] = 0

    class V2(Input):
        value: Union[str, int] = 0

    assert_backwards_compatible(V1, V2)

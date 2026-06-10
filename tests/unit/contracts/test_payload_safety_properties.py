"""Property-based tests for contract payload-safety validation.

``validate_payload_safety`` is the build-time guard that keeps contract
fields inside Temporal's 2 MB payload limit (ADR-0008).  These properties
generate contract classes combinatorially and pin the invariant:

* a contract whose every field is bounded (scalars, ``Annotated[...,
  MaxItems(n)]`` collections) ALWAYS validates
* injecting any single unbounded field (``dict[str, Any]``, bare ``list``,
  ``Any``, ``bytes``, bounded collections of unbounded elements) ALWAYS
  raises ``PayloadSafetyError`` naming exactly that field
* the documented escape hatches (``skip_fields``, ``_allow_unbounded_fields``)
  neutralize the rejection
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from application_sdk.contracts.base import PayloadSafetyError, validate_payload_safety
from application_sdk.contracts.types import FileReference, MaxItems

# Payload-safe field types: scalars and explicitly bounded collections.
_SAFE_TYPES: tuple[Any, ...] = (
    str,
    int,
    float,
    bool,
    str | None,
    FileReference,
    Annotated[list[str], MaxItems(10)],
    Annotated[list[int], MaxItems(100)],
    Annotated[dict[str, str], MaxItems(10)],
    Annotated[dict[str, int], MaxItems(5)],
)

# Forbidden field types: unbounded or unvalidatable payloads.
_UNSAFE_TYPES: tuple[Any, ...] = (
    Any,
    bytes,
    bytearray,
    list[str],
    list[int],
    dict[str, str],
    dict[str, Any],
    list[dict[str, Any]],
    # Bounded collection of UNBOUNDED elements is still forbidden.
    Annotated[list[dict[str, Any]], MaxItems(10)],
)

# Field names: valid identifiers that aren't skipped by the validator
# (no "_" prefix, no "model_" prefix).
_field_name = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True).filter(
    lambda name: not name.startswith("model")
)

_field_names = st.lists(_field_name, min_size=1, max_size=6, unique=True)


def _make_contract_class(annotations: dict[str, Any]) -> type:
    """Build a contract-shaped class with the given field annotations."""
    return type("GeneratedContract", (), {"__annotations__": dict(annotations)})


@st.composite
def _safe_annotations(draw) -> dict[str, Any]:
    names = draw(_field_names)
    return {name: draw(st.sampled_from(_SAFE_TYPES)) for name in names}


@given(annotations=_safe_annotations())
@settings(max_examples=50)
def test_all_bounded_fields_always_validate(annotations: dict[str, Any]) -> None:
    """Any combination of bounded-annotated fields passes validation."""
    cls = _make_contract_class(annotations)
    validate_payload_safety(cls)  # must not raise


@given(
    annotations=_safe_annotations(),
    unsafe_name=_field_name,
    unsafe_type=st.sampled_from(_UNSAFE_TYPES),
    position=st.integers(min_value=0, max_value=6),
)
@settings(max_examples=50)
def test_any_single_unbounded_field_is_rejected_by_name(
    annotations: dict[str, Any],
    unsafe_name: str,
    unsafe_type: Any,
    position: int,
) -> None:
    """One unbounded field anywhere in the contract fails, naming that field."""
    safe_items = [(k, v) for k, v in annotations.items() if k != unsafe_name]
    idx = min(position, len(safe_items))
    mixed = dict(safe_items[:idx]) | {unsafe_name: unsafe_type} | dict(safe_items[idx:])
    cls = _make_contract_class(mixed)

    with pytest.raises(PayloadSafetyError) as excinfo:
        validate_payload_safety(cls)
    assert excinfo.value.field_name == unsafe_name


@given(
    annotations=_safe_annotations(),
    unsafe_name=_field_name,
    unsafe_type=st.sampled_from(_UNSAFE_TYPES),
)
@settings(max_examples=25)
def test_skip_fields_neutralizes_the_unsafe_field(
    annotations: dict[str, Any],
    unsafe_name: str,
    unsafe_type: Any,
) -> None:
    """skip_fields exempts framework-owned fields from the check."""
    mixed = {k: v for k, v in annotations.items() if k != unsafe_name}
    mixed[unsafe_name] = unsafe_type
    cls = _make_contract_class(mixed)

    validate_payload_safety(cls, skip_fields={unsafe_name})  # must not raise


@given(
    unsafe_name=_field_name,
    unsafe_type=st.sampled_from(_UNSAFE_TYPES),
)
@settings(max_examples=25)
def test_allow_unbounded_fields_opt_out_disables_validation(
    unsafe_name: str,
    unsafe_type: Any,
) -> None:
    """The _allow_unbounded_fields opt-out skips the whole check."""
    cls = type(
        "GeneratedContract",
        (),
        {
            "__annotations__": {unsafe_name: unsafe_type},
            "_allow_unbounded_fields": True,
        },
    )
    validate_payload_safety(cls)  # must not raise

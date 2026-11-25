"""Runtime contract backwards-compatibility helpers.

Provides type normalization and field comparison utilities for use in tests
and application startup validation.  The conformance suite (B005/B006) applies
the same canonical form to source ASTs — union arms sorted and deduplicated
with ``None`` last, typing aliases lowered, ``Annotated`` metadata stripped —
and a parity test pins the two to identical strings; this module operates on
live Pydantic classes and is suitable for runtime assertions and integration
tests.

Typical usage (in app tests)::

    from application_sdk.contracts.compat import assert_backwards_compatible

    def test_contract_is_backwards_compatible():
        assert_backwards_compatible(V1Input, V2Input)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.contracts.base import (
    _incompatibilities,
    canonical_type_str,
    validate_is_contract,
)
from application_sdk.errors.leaves import InternalError

__all__ = [
    "CompatibilityError",
    "Incompatibility",
    "assert_backwards_compatible",
    "canonical_type_str",
    "check_backwards_compatible",
    "field_lifecycle",
]


# ── Field lifecycle ───────────────────────────────────────────────────────────


def field_lifecycle(cls: type, field_name: str) -> str:
    """Return the lifecycle status of a contract field: ``"active"``, ``"deprecated"``, or ``"sunset"``.

    Reads the Pydantic ``FieldInfo`` metadata that the contract toolkit emits on
    generated fields.  Hand-authored contracts can also use these markers directly:

    - ``deprecated=True`` on ``Field(...)`` → ``"deprecated"``
    - ``json_schema_extra={"x-lifecycle": "sunset"}`` on ``Field(...)`` → ``"sunset"``
    """
    validate_is_contract(cls)
    fi = cls.model_fields.get(field_name)
    if fi is None:
        return "active"
    extra = fi.json_schema_extra or {}
    if isinstance(extra, dict) and extra.get("x-lifecycle") == "sunset":
        return "sunset"
    if fi.deprecated:
        return "deprecated"
    return "active"


# ── Backwards-compatibility check ────────────────────────────────────────────


@dataclass(frozen=True)
class Incompatibility:
    """One backwards-incompatibility between two contract versions."""

    field: str
    reason: str


@dataclass(kw_only=True)
class CompatibilityError(InternalError):
    """Raised by :func:`assert_backwards_compatible` on incompatible contracts.

    An :class:`InternalError` (APP_OWNER): the app's own contract change would
    break payloads an older version wrote.
    """

    code: ClassVar[str] = "INTERNAL_CONTRACT_NOT_BACKWARDS_COMPATIBLE"

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ keeps the positional-issues signature callers use.
    def __init__(self, issues: list[Incompatibility]) -> None:
        lines = "\n".join(f"  - {i.field}: {i.reason}" for i in issues)
        InternalError.__init__(
            self, message=f"Contract is not backwards-compatible:\n{lines}"
        )
        #: The incompatibilities found. Not a dataclass field, so it stays off
        #: the failure envelope's evidence.
        self.issues = issues


def check_backwards_compatible(
    old_cls: type,
    new_cls: type,
) -> tuple[bool, list[Incompatibility]]:
    """Check whether *new_cls* is backwards-compatible with *old_cls*.

    Backwards-compatibility rules:
    - Every field in *old_cls* must exist in *new_cls* with the same canonical type.
    - New fields in *new_cls* must have a default value.

    Types compare by :func:`canonical_type_str`, the same canonical form the
    conformance suite's B005/B006 contract ledger uses, so annotations that
    accept the same payloads are equal: ``Optional[str]`` and ``str | None``,
    ``Union[int, str]`` and ``Union[str, int]``, ``Annotated[List[int], ...]``
    and ``list[int]``.

    Returns a ``(compatible, issues)`` tuple.

    Raises:
        ContractDeclarationError: If either class is not a contract class.
    """
    issues = [
        Incompatibility(field=name, reason=reason)
        for name, reason in _incompatibilities(old_cls, new_cls)
    ]
    return len(issues) == 0, issues


def assert_backwards_compatible(old_cls: type, new_cls: type) -> None:
    """Assert *new_cls* is backwards-compatible with *old_cls*, raising on failure.

    Convenience wrapper around :func:`check_backwards_compatible` for use in
    tests and startup assertions.

    Raises:
        CompatibilityError: if any incompatibility is detected.
    """
    ok, issues = check_backwards_compatible(old_cls, new_cls)
    if not ok:
        raise CompatibilityError(issues)

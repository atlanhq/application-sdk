"""Runtime contract backwards-compatibility helpers.

Provides type normalization and field comparison utilities for use in tests
and application startup validation.  The conformance suite (B005/B006) uses
an AST-based variant of the same logic; this module operates on live Pydantic
classes and is suitable for runtime assertions and integration tests.

Typical usage (in app tests)::

    from application_sdk.contracts.compat import assert_backwards_compatible

    def test_contract_is_backwards_compatible():
        assert_backwards_compatible(V1Input, V2Input)
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from typing import Any, get_type_hints

from application_sdk.contracts.base import has_default, validate_is_contract

__all__ = [
    "CompatibilityError",
    "Incompatibility",
    "assert_backwards_compatible",
    "canonical_type_str",
    "check_backwards_compatible",
    "field_lifecycle",
]

# ── Type normalization ────────────────────────────────────────────────────────

# Typing aliases that should be lowercased when normalizing.
_ALIAS_MAP: dict[str, str] = {
    "List": "list",
    "Dict": "dict",
    "Tuple": "tuple",
    "Set": "set",
    "FrozenSet": "frozenset",
    "Type": "type",
}


def _normalize_args(args: tuple[Any, ...]) -> str:
    """Render type arguments as a comma-separated string."""
    return ", ".join(canonical_type_str(a) for a in args)


def canonical_type_str(tp: Any) -> str:
    """Return a normalized string representation of a type annotation.

    Normalizations applied:
    - ``Optional[X]`` / ``X | None`` → ``X | None``
    - ``Union[X, Y]`` → ``X | Y``
    - ``List[X]`` → ``list[X]``, ``Dict[K, V]`` → ``dict[K, V]``, etc.
    - ``Annotated[X, ...]`` → ``X`` (metadata stripped)
    - ``typing.Any`` → ``Any``

    This produces a stable string suitable for ledger comparison across Python
    versions that may use different internal type representations.
    """
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    # Annotated[X, ...] — strip metadata, recurse on X
    if origin is typing.Annotated:
        return canonical_type_str(args[0])

    # Union / Optional
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        if not non_none:
            return "None"
        parts = [canonical_type_str(a) for a in non_none]
        result = " | ".join(parts)
        if has_none:
            result = f"{result} | None"
        return result

    # PEP 604 union (Python 3.10+): types.UnionType
    if hasattr(types, "UnionType") and isinstance(tp, types.UnionType):
        args = typing.get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        parts = [canonical_type_str(a) for a in non_none]
        result = " | ".join(parts)
        if has_none:
            result = f"{result} | None"
        return result

    # Generic aliases: list[X], dict[K, V], etc.
    if origin is not None:
        origin_name = getattr(origin, "__name__", None) or str(origin)
        origin_name = _ALIAS_MAP.get(origin_name, origin_name)
        if args:
            return f"{origin_name}[{_normalize_args(args)}]"
        return origin_name

    # Plain type
    if isinstance(tp, type):
        if tp is type(None):
            return "None"
        return tp.__name__

    # typing.Any and similar singletons
    if tp is Any:
        return "Any"

    # Fallback: string representation
    raw = str(tp)
    # Replace legacy typing aliases in the raw string
    for old, new in _ALIAS_MAP.items():
        raw = raw.replace(f"typing.{old}[", f"{new}[")
    return raw


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


class CompatibilityError(Exception):
    """Raised by :func:`assert_backwards_compatible` on incompatible contracts."""

    def __init__(self, issues: list[Incompatibility]) -> None:
        self.issues = issues
        lines = "\n".join(f"  - {i.field}: {i.reason}" for i in issues)
        super().__init__(f"Contract is not backwards-compatible:\n{lines}")


def check_backwards_compatible(
    old_cls: type,
    new_cls: type,
) -> tuple[bool, list[Incompatibility]]:
    """Check whether *new_cls* is backwards-compatible with *old_cls*.

    Backwards-compatibility rules:
    - Every field in *old_cls* must exist in *new_cls* with the same canonical type.
    - New fields in *new_cls* must have a default value.

    Unlike :func:`~application_sdk.contracts.base.is_backwards_compatible`, this
    function uses :func:`canonical_type_str` for type comparison so that
    ``Optional[str]`` and ``str | None`` are treated as equivalent.

    Returns a ``(compatible, issues)`` tuple.
    """
    validate_is_contract(old_cls, "old contract")
    validate_is_contract(new_cls, "new contract")

    old_hints = get_type_hints(old_cls)
    new_hints = get_type_hints(new_cls)
    old_fields = {name: old_hints.get(name, Any) for name in old_cls.model_fields}
    new_fields = {name: new_hints.get(name, Any) for name in new_cls.model_fields}

    issues: list[Incompatibility] = []

    for name, old_tp in old_fields.items():
        if name not in new_fields:
            issues.append(Incompatibility(field=name, reason="field was removed"))
        else:
            old_canon = canonical_type_str(old_tp)
            new_canon = canonical_type_str(new_fields[name])
            if old_canon != new_canon:
                issues.append(
                    Incompatibility(
                        field=name,
                        reason=f"type changed from '{old_canon}' to '{new_canon}'",
                    )
                )

    for name in new_fields:
        if name not in old_fields and not has_default(new_cls, name):
            issues.append(
                Incompatibility(
                    field=name,
                    reason="new required field (no default value) would break old callers",
                )
            )

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

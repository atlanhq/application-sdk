"""Msgspec encoder and type converter for Temporal.

Provides msgspec-aware JSON encoder and type converter so that msgspec.Struct
instances (including pyatlan Assets and admin types) serialize/deserialize
transparently through Temporal's workflow and activity system.

Everything else falls through to Temporal's default converters.

The msgspec decoders are pre-warmed at module import time to avoid issues
with Temporal's workflow sandbox, which restricts annotation evaluation.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

from temporalio.converter import AdvancedJSONEncoder, JSONTypeConverter

# msgspec is an optional dependency (needed for pyatlan Struct type support)
try:
    import msgspec  # type: ignore[import-untyped]

    MSGSPEC_AVAILABLE = True
except ImportError:
    msgspec = None  # type: ignore[assignment]
    MSGSPEC_AVAILABLE = False

# pyatlan imports are optional — only needed when pyatlan extras are installed
PYATLAN_AVAILABLE = False
get_serde: Any = None
get_type: Any = None

try:
    from pyatlan_v9.model.serde import (
        get_serde as _get_serde,  # type: ignore[import-untyped]
    )
    from pyatlan_v9.model.transform import (
        get_type as _get_type,  # type: ignore[import-untyped]
    )

    get_serde = _get_serde
    get_type = _get_type
    PYATLAN_AVAILABLE = True
except ImportError:
    pass

# Pre-warm the msgspec decoder cache at module import time.
# Critical because Temporal's workflow sandbox restricts annotation evaluation.
if (
    PYATLAN_AVAILABLE
    and MSGSPEC_AVAILABLE
    and get_serde is not None
    and msgspec is not None
):
    _prewarm_serde = get_serde()
    try:
        from pyatlan_v9.model.assets.asset import (
            AssetNested,  # type: ignore[import-untyped]
        )

        if AssetNested not in _prewarm_serde._decoders:
            _prewarm_serde._decoders[AssetNested] = msgspec.json.Decoder(AssetNested)
    except Exception:
        pass


class MsgspecJSONEncoder(AdvancedJSONEncoder):
    """JSON encoder that uses msgspec for all msgspec.Struct types."""

    def default(self, o: Any) -> Any:
        """Convert msgspec.Struct instances using msgspec direct encoding."""
        if MSGSPEC_AVAILABLE and msgspec is not None and isinstance(o, msgspec.Struct):
            return msgspec.to_builtins(o)
        return super().default(o)


class MsgspecTypeConverter(JSONTypeConverter):
    """Type converter that handles msgspec.Struct types and plain Python dataclasses.

    Handles three cases:
    1. pyatlan Asset types (have typeName field) - uses type registry for polymorphism
    2. Other msgspec.Struct types (e.g., AtlanGroup) - uses type hint directly
    3. Plain Python dataclasses (e.g. Input/Output subclasses) - uses msgspec.convert
    """

    def to_typed_value(self, hint: type, value: Any) -> Any:
        """Convert dicts to typed instances."""
        if not isinstance(value, dict):
            return JSONTypeConverter.Unhandled

        if not MSGSPEC_AVAILABLE or msgspec is None:
            # Without msgspec, only handle dataclasses via standard Temporal converters
            return JSONTypeConverter.Unhandled

        # Case 1: pyatlan Asset types with typeName (polymorphic)
        if PYATLAN_AVAILABLE and get_type is not None and "typeName" in value:
            type_name = value["typeName"]
            cls = get_type(type_name)
            return msgspec.convert(value, cls, strict=False)

        # Case 2: Other msgspec.Struct types (use type hint directly)
        target_type: Any = hint
        if not isinstance(hint, type):
            for arg in typing.get_args(hint):
                if isinstance(arg, type) and issubclass(arg, msgspec.Struct):
                    target_type = arg
                    break

        try:
            if isinstance(target_type, type) and issubclass(
                target_type, msgspec.Struct
            ):
                return msgspec.convert(value, target_type, strict=False)
        except TypeError:
            pass

        # Case 3: Plain Python dataclasses (Input/Output subclasses)
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            return msgspec.convert(value, hint, strict=False)

        if not isinstance(hint, type):
            import types as _builtin_types

            _union_type = getattr(_builtin_types, "UnionType", None)
            origin = typing.get_origin(hint)
            is_union = origin is typing.Union or (
                _union_type is not None and isinstance(hint, _union_type)
            )
            if is_union and any(
                isinstance(arg, type) and dataclasses.is_dataclass(arg)
                for arg in typing.get_args(hint)
                if arg is not type(None)
            ):
                return msgspec.convert(value, hint, strict=False)

        return JSONTypeConverter.Unhandled

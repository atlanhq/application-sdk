"""Deprecated: part of the retired scale data generator.

No app and no CI job uses the generator, so it leaves the package. The
implementation lives in :mod:`application_sdk.testing._scale_data_generator`;
every name here still resolves to the same object, with a ``DeprecationWarning``
on access.

.. deprecated:: 3.56
    Use faker directly in your own fixture script instead. Will be removed in
    v4.0.0 along with the ``[scale_data_generator]`` extra (FND-4660).
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing._scale_data_generator.output_handler.json_handler import (  # noqa: F401
        JsonFormatHandler,
        OutputFormatHandler,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the private implementation's object itself, so a patch or an
#: ``isinstance`` check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "OutputFormatHandler": (
        "faker directly in your own fixture script",
        "the scale data generator has no callers and leaves the package (FND-4660)",
    ),
    "JsonFormatHandler": (
        "faker directly in your own fixture script",
        "the scale data generator has no callers and leaves the package (FND-4660)",
    ),
}


#: name -> (defining module, attribute): where ``__getattr__`` fetches each name
#: from. The symbol gate reads it too, to follow each name to its definition so
#: a moved class keeps its methods on this module's surface.
_LAZY: dict[str, tuple[str, str]] = {
    "OutputFormatHandler": (
        "application_sdk.testing._scale_data_generator.output_handler.json_handler",
        "OutputFormatHandler",
    ),
    "JsonFormatHandler": (
        "application_sdk.testing._scale_data_generator.output_handler.json_handler",
        "JsonFormatHandler",
    ),
}


#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. A literal, because the symbol gate reads it statically.
__all__ = [
    "OutputFormatHandler",
    "JsonFormatHandler",
]


def __getattr__(name: str) -> object:
    """Serve the moved names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module, attr = _LAZY[name]
    return getattr(importlib.import_module(module), attr)

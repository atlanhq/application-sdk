"""Deprecated CLI entry point: ``python -m application_sdk.testing.parity``.

Still runs, with a ``DeprecationWarning``. The comparator itself is not
deprecated: call :func:`application_sdk.testing.parity.run_comparison` and
:func:`application_sdk.testing.parity.generate_markdown` (or
:func:`~application_sdk.testing.parity.generate_json_report`) from a short
script of your own instead. The implementation lives in
:mod:`application_sdk.testing.parity._cli`.

.. deprecated:: 3.56
    Use the library API above. Will be removed in v4.0.0 (FND-4660).
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

# The supported library API, re-exported eagerly and NOT deprecated. Serving
# these through the alias below would put them in the deprecated-symbol manifest
# under this module, and B001 matches a manifest entry against imports from any
# parent package, so ``from application_sdk.testing.parity import
# run_comparison`` (the documented replacement) would be flagged deprecated.
from application_sdk.testing.parity import (
    generate_json_report,
    generate_markdown,
    run_comparison,
)

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing.parity._cli import (  # noqa: F401
        get_logger,
        logger,
        main,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the private implementation's object itself, so a patch or an
#: ``isinstance`` check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "main": (
        "application_sdk.testing.parity.run_comparison with generate_markdown or generate_json_report",
        "the parity CLI entry point is retired; the library API stays (FND-4660)",
    ),
    "get_logger": (
        "application_sdk.observability.logger_adaptor.get_logger",
        "the parity CLI entry point is retired; the library API stays (FND-4660)",
    ),
    "logger": (
        "a logger of your own from application_sdk.observability.logger_adaptor.get_logger",
        "the parity CLI entry point is retired; the library API stays (FND-4660)",
    ),
}


#: name -> (defining module, attribute): where ``__getattr__`` fetches each name
#: from. The symbol gate reads it too, to follow each name to its definition so
#: a moved class keeps its methods on this module's surface.
_LAZY: dict[str, tuple[str, str]] = {
    "main": ("application_sdk.testing.parity._cli", "main"),
    "get_logger": ("application_sdk.testing.parity._cli", "get_logger"),
    "logger": ("application_sdk.testing.parity._cli", "logger"),
}


#: Kept so ``from <this module> import *`` still binds every name: a star import
#: reads ``__all__`` and resolves each deprecated entry through ``__getattr__``,
#: so those warn. A literal, because the symbol gate reads it statically.
__all__ = [
    "main",
    "get_logger",
    "logger",
    "run_comparison",
    "generate_markdown",
    "generate_json_report",
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


if __name__ == "__main__":
    warnings.warn(
        "python -m application_sdk.testing.parity is deprecated; use "
        "application_sdk.testing.parity.run_comparison with generate_markdown or "
        "generate_json_report from a script of your own instead. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=1,
    )
    from application_sdk.testing.parity._cli import main as _main  # noqa: PLC0415

    _main()

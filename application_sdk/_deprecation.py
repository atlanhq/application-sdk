"""The one deprecation mechanism the SDK uses.

Three shapes cover every deprecation, and each has one helper here:

* **A def or a class** — :func:`deprecated` (PEP 702, re-exported from
  ``typing_extensions``). Type checkers flag the call site and Python warns on
  use.
* **A deprecated way of calling something** (a parameter, a default, a mode) —
  :func:`warn_deprecated`. It builds the notice in one fixed format, so every
  notice names a replacement and a removal version.
* **A module-level name** (a moved or removed constant, a re-export) —
  :func:`deprecated_module_getattr`, a PEP 562 ``__getattr__`` built from a
  ``_DEPRECATED_CONSTANTS`` table.

Standard library and ``typing_extensions`` only: ``application_sdk.constants``
uses the module-level shim, so nothing here may import from the SDK.

The conformance suite reads these statically. A ``warn_deprecated(...)`` call
and a ``__getattr__ = deprecated_module_getattr(...)`` assignment are both
graded by B002/B003 and recorded in the deprecated-symbol manifest, so pass the
name, replacement and ``remove_in`` as literals. A value held in a variable
cannot be read without running the code. See ``docs/standards/symbols.md``.
"""

from __future__ import annotations

import importlib
import re
import warnings
from collections.abc import Callable, Mapping

from typing_extensions import deprecated

__all__ = [
    "deprecated",
    "deprecated_module_getattr",
    "deprecation_message",
    "lazy_resolver",
    "warn_deprecated",
]

#: A removal version is a full ``major.minor.patch``. ``4.0`` and ``4.0.0``
#: name the same release, and allowing both made the SDK use both.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

#: One ``_DEPRECATED_CONSTANTS`` entry: ``(replacement, why)``, or a complete
#: notice string for a name with no replacement to point at.
DeprecatedName = tuple[str, str] | str


def _check_version(remove_in: str) -> None:
    if not _VERSION_RE.fullmatch(remove_in):
        raise ValueError(
            f"remove_in must be a full major.minor.patch version such as "
            f"'4.0.0', not {remove_in!r}"
        )


def deprecation_message(
    name: str, replacement: str, remove_in: str, *, note: str | None = None
) -> str:
    """The notice text :func:`warn_deprecated` emits.

    Args:
        name: What is deprecated, as the caller wrote it.
        replacement: What to use instead.
        remove_in: The release that removes it, as ``major.minor.patch``.
        note: Why, or what changes; appended after the replacement.

    Raises:
        ValueError: *remove_in* is not a ``major.minor.patch`` version.
    """
    _check_version(remove_in)
    why = f" — {note.rstrip(' .')}" if note else ""
    return (
        f"{name} is deprecated; use {replacement} instead{why}. "
        f"Will be removed in v{remove_in}."
    )


def _emit(message: str, stacklevel: int) -> None:
    # The notice is graded where warn_deprecated / deprecated_module_getattr is
    # called with literals; this call only forwards it.
    # conformance: ignore[B002] forwards a notice graded at its call site
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel + 1)


def warn_deprecated(
    name: str,
    replacement: str,
    remove_in: str,
    *,
    note: str | None = None,
    stacklevel: int = 2,
) -> None:
    """Emit a ``DeprecationWarning`` in the SDK's one notice format.

    *stacklevel* means what it means to :func:`warnings.warn` called from the
    same place: the default ``2`` points at the caller of the function that
    calls this.

    Raises:
        ValueError: *remove_in* is not a ``major.minor.patch`` version.
    """
    _emit(deprecation_message(name, replacement, remove_in, note=note), stacklevel + 1)


def _import_dotted(path: str) -> object:
    """Resolve ``package.module.Attr[.attr]`` by importing the longest module prefix."""
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            target: object = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for attr in parts[split:]:
            target = getattr(target, attr)
        return target
    raise ImportError(f"cannot resolve {path!r} to an importable object")


def lazy_resolver(lazy: Mapping[str, tuple[str, str]]) -> Callable[[str], object]:
    """A *resolve* for :func:`deprecated_module_getattr` that reads a ``_LAZY`` map.

    *lazy* maps each name to ``(module, attribute)``; the module is imported on
    first access, so serving a deprecated name costs nothing until it is used.
    """

    def resolve(name: str) -> object:
        module, attr = lazy[name]
        return getattr(importlib.import_module(module), attr)

    return resolve


def deprecated_module_getattr(
    module: str,
    table: Mapping[str, DeprecatedName],
    *,
    remove_in: str,
    resolve: Callable[[str], object] | None = None,
    fallback: Callable[[str], object] | None = None,
) -> Callable[[str], object]:
    """Build a module ``__getattr__`` (PEP 562) that serves deprecated names.

    Use it as ``__getattr__ = deprecated_module_getattr(__name__,
    _DEPRECATED_CONSTANTS, remove_in="4.0.0")``. Each access to a name in
    *table* warns at the caller's line and returns the value. A real binding of
    the same name at module scope would resolve first and never warn, so do not
    keep one.

    Args:
        module: The module's ``__name__``, for the ``AttributeError`` message.
        table: ``_DEPRECATED_CONSTANTS``. A ``(replacement, why)`` entry is
            formatted by :func:`deprecation_message`. A string entry is the
            complete notice, for a name with nothing to replace it; it starts
            with the name, and is emitted qualified by *module*.
        remove_in: The release that removes every name in *table*.
        resolve: Returns the value for a name. Defaults to importing the
            entry's replacement as a dotted path, which fits a name that moved.
        fallback: Called for a name not in *table*, for a module whose
            ``__getattr__`` also serves lazy imports. Without one, such a
            name raises ``AttributeError``.

    Raises:
        ValueError: *remove_in* is not a ``major.minor.patch`` version.
    """
    _check_version(remove_in)

    def __getattr__(name: str) -> object:
        entry = table.get(name)
        if entry is None:
            if fallback is not None:
                return fallback(name)
            raise AttributeError(f"module {module!r} has no attribute {name!r}")
        if isinstance(entry, str):
            _emit(f"{module}.{entry}", 2)
            if resolve is None:
                raise AssertionError(f"{module}.{name} has no resolver")
            return resolve(name)
        replacement, note = entry
        _emit(deprecation_message(name, replacement, remove_in, note=note), 2)
        if resolve is not None:
            return resolve(name)
        return _import_dotted(replacement)

    return __getattr__

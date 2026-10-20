"""Which distribution is installed: the full SDK, or only the api subset.

``atlan-application-sdk-api`` ships a listed subset of ``application_sdk/`` (see
``packages/api/api-files.txt``) for the consolidated API host. A few listed
files reach worker-only code lazily (the object store, Temporal, the worker's
infrastructure). Each such import is wrapped so that, on an api-only install,
it takes a stated fallback instead of raising at call time — and so that, with
the full SDK installed, a missing module is still a real error and still raises.
:func:`api_only_install` is the one test that tells the two apart.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
from collections.abc import Collection, Mapping, MutableMapping
from types import ModuleType
from typing import Any

#: A module the full SDK always ships and the api distribution never does.
_WORKER_SENTINEL = "application_sdk.main"

#: The modules the :func:`worker_only_missing` guards import, none of them shipped
#: by the api distribution: the object store (``contracts.types``,
#: ``credentials.utils``, ``common.utils``, ``observability.observability``), the
#: worker's infrastructure (``handler.context``, ``observability.observability``),
#: the SDK's structured logger (``_logging``) and Temporal
#: (``observability.utils``). A guard falls back only when one of these (or a
#: submodule of one) is what is missing; anything else is a real error. A new
#: guard over another worker-only module adds that module here.
_WORKER_ONLY_PREFIXES: tuple[str, ...] = (
    "application_sdk.infrastructure",
    "application_sdk.observability",
    "application_sdk.storage",
    "temporalio",
)


@functools.cache
def api_only_install() -> bool:
    """True when only ``atlan-application-sdk-api`` is installed (the API host)."""
    return importlib.util.find_spec(_WORKER_SENTINEL) is None


def worker_only_missing(exc: ModuleNotFoundError) -> bool:
    """True when ``exc`` is an api-only install lacking a worker-only module.

    Use it in ``except ModuleNotFoundError as exc: if not worker_only_missing(exc):
    raise``. With the full SDK installed it is always False, so the error
    propagates exactly as it did before the split. On an api-only install it is
    True only when the missing module is one of :data:`_WORKER_ONLY_PREFIXES`;
    a missing api file or third-party dependency still raises.
    """
    if not api_only_install():
        return False
    missing = exc.name or ""
    return any(
        missing == prefix or missing.startswith(prefix + ".")
        for prefix in _WORKER_ONLY_PREFIXES
    )


def import_submodule(package: str, name: str) -> ModuleType | None:
    """``package.name`` when it names an existing submodule, else ``None``.

    A package whose ``__init__`` imports lazily (a ``__getattr__`` over a
    ``_LAZY`` map) no longer has its submodules as attributes until something
    imports them. :func:`lazy_getattr` calls this for the submodules the package
    used to import eagerly, so ``import pkg; pkg.sub`` keeps working for them.
    """
    if not name.isidentifier() or name.startswith("__"):
        return None
    full = f"{package}.{name}"
    try:
        spec = importlib.util.find_spec(full)
    except (ImportError, ValueError):
        return None
    return importlib.import_module(full) if spec is not None else None


def _names_missing_module(exc: ModuleNotFoundError, module: str) -> bool:
    """Whether ``exc`` is *module* itself (or a parent package) being absent."""
    missing = exc.name or ""
    return bool(missing) and (module == missing or module.startswith(f"{missing}."))


def lazy_getattr(
    module_name: str,
    name: str,
    lazy_map: Mapping[str, tuple[str, str]],
    globals_: MutableMapping[str, Any],
    submodules: Collection[str] = (),
) -> Any:
    """The PEP 562 ``__getattr__`` body of a package that imports lazily.

    *lazy_map* maps a public name to ``(module, attribute)``; the first access
    imports it and caches it in the package's *globals_*. *submodules* are the
    submodules the package imported eagerly before it went lazy, so
    ``import pkg; pkg.sub`` keeps working for those and only those: any other
    submodule is an attribute only once something imports it, as with any
    package (so ``hasattr(pkg, "sub")`` does not import it).
    ``from pkg import sub`` needs no entry; the import system loads it.

    A name whose module is not installed (on an api-only install the worker's
    modules are absent) raises :class:`AttributeError` chained to the import
    error, so ``hasattr`` and ``getattr(pkg, name, default)`` answer normally.
    A module that is installed but fails to import raises its own error
    unchanged: that is a real bug, and ``from pkg import name`` would otherwise
    report only "cannot import name".
    """
    target = lazy_map.get(name)
    if target is not None:
        try:
            module = importlib.import_module(target[0])
        except ModuleNotFoundError as exc:
            if not _names_missing_module(exc, target[0]):
                raise
            raise AttributeError(
                f"module {module_name!r} has no attribute {name!r}: "
                f"{target[0]} is not installed"
            ) from exc
        value = getattr(module, target[1])
        globals_[name] = value
        return value
    if name in submodules:
        submodule = import_submodule(module_name, name)
        if submodule is not None:
            return submodule
    raise AttributeError(f"module {module_name!r} has no attribute {name!r}")

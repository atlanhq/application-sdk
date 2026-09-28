"""Deprecated alias of :mod:`application_sdk_api.handler.context` (removal in v4.0).

The handler surface lives in the ``atlan-application-sdk-api`` package so the
consolidated API host can serve an app's handler without this distribution.
Import from ``application_sdk_api.handler.context`` instead. Every name here resolves to the object in
``application_sdk_api.handler.context`` (with a ``DeprecationWarning``), so behaviour is unchanged.

Do not define anything in this module. ``guard_api_shims.py`` fails CI if it holds
more than this re-export; make changes in ``packages/api``.
"""

from __future__ import annotations

import importlib as _importlib
import warnings as _warnings
from typing import TYPE_CHECKING as _TYPE_CHECKING
from typing import Any as _Any

import application_sdk_api.handler.context as _src

if _TYPE_CHECKING:
    from application_sdk_api.handler.context import *  # noqa: F401,F403

_EXTRA: dict[str, str] = {
    "bind_invocation_context": "application_sdk.handler.invocation",
}
#: Worker-surface names (App configuration, event triggers) that are not part
#: of the handler surface and are not deprecated on this path.
_NOT_DEPRECATED: frozenset[str] = frozenset({"bind_invocation_context"})
__all__ = list(
    getattr(_src, "__all__", [n for n in dir(_src) if not n.startswith("_")])
) + list(_EXTRA)  # noqa: PLE0605


def __getattr__(name: str) -> _Any:
    if name in _EXTRA:
        value = getattr(_importlib.import_module(_EXTRA[name]), name)
    else:
        value = getattr(_src, name)
    if not name.startswith("_") and name not in _NOT_DEPRECATED:
        _warnings.warn(
            f"application_sdk.handler.context.{name} is deprecated and will be removed in v4.0; "
            f"import it from application_sdk_api.handler.context",
            DeprecationWarning,
            stacklevel=2,
        )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_src)))

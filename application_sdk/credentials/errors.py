"""Re-export of :mod:`application_sdk_api.credentials.errors`.

This code lives in the ``atlan-application-sdk-api`` package so the consolidated
API host and the worker share one implementation. ``application_sdk.credentials.errors`` is the SDK's
first-class path to the same objects and is not deprecated.

Do not define anything in this module. ``guard_api_shims.py`` fails CI if it holds
more than this re-export; make changes in ``packages/api``.
"""

from __future__ import annotations

import importlib as _importlib
from typing import Any as _Any

import application_sdk_api.credentials.errors as _src
from application_sdk_api.credentials.errors import *  # noqa: F401,F403

_EXTRA: dict[str, str] = {}
__all__ = list(
    getattr(_src, "__all__", [n for n in dir(_src) if not n.startswith("_")])
) + list(_EXTRA)  # noqa: PLE0605


def __getattr__(name: str) -> _Any:
    if name in _EXTRA:
        value = getattr(_importlib.import_module(_EXTRA[name]), name)
    else:
        value = getattr(_src, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(dir(_src)))

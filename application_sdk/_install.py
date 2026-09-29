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
import importlib.util

#: A module the full SDK always ships and the api distribution never does.
_WORKER_SENTINEL = "application_sdk.main"


@functools.cache
def api_only_install() -> bool:
    """True when only ``atlan-application-sdk-api`` is installed (the API host)."""
    return importlib.util.find_spec(_WORKER_SENTINEL) is None


def worker_only_missing(exc: ModuleNotFoundError) -> bool:
    """True when ``exc`` is an api-only install lacking a worker-only module.

    Use it in ``except ModuleNotFoundError as exc: if not worker_only_missing(exc):
    raise``. With the full SDK installed it is always False, so the error
    propagates exactly as it did before the split.
    """
    return api_only_install()

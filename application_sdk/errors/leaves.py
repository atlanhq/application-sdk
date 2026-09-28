"""Re-export of :mod:`application_sdk_api.errors.leaves`.

The SDK's error taxonomy lives in the ``atlan-application-sdk-api`` package so the
consolidated API host and the worker share one set of classes. ``application_sdk.errors.leaves``
is the SDK's first-class path to the same objects and is not deprecated: every
name here *is* the object in ``application_sdk_api.errors.leaves``, so ``isinstance``, ``except``
and subclassing behave identically through either path.

Do not define anything in this module. ``guard_api_shims.py`` fails CI if it holds
more than this re-export; make changes in ``packages/api``.
"""

import application_sdk_api.errors.leaves as _src
from application_sdk_api.errors.leaves import *  # noqa: F401,F403


def __getattr__(name: str):
    # Private names (``_BASE_FIELDS`` and the like) are not covered by ``*``.
    return getattr(_src, name)

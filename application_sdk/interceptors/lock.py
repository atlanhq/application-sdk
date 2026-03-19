"""Deprecated: use application_sdk.execution._temporal.interceptors.lock instead."""

import warnings

warnings.warn(
    "application_sdk.interceptors.lock is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.execution._temporal.interceptors.lock instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.execution._temporal.interceptors.lock import *  # noqa: E402, F401, F403
from application_sdk.execution._temporal.interceptors.lock import (  # noqa: E402, F401
    RedisLockInterceptor,
    RedisLockOutboundInterceptor,
)

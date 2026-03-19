"""Deprecated: use application_sdk.execution._temporal.lock_activities instead."""

import warnings

warnings.warn(
    "application_sdk.activities.lock_management is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.execution._temporal.lock_activities instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.execution._temporal.lock_activities import *  # noqa: E402, F401, F403
from application_sdk.execution._temporal.lock_activities import (  # noqa: E402, F401
    acquire_distributed_lock,
    release_distributed_lock,
)

"""Deprecated: use application_sdk.common.models instead."""

import warnings

warnings.warn(
    "application_sdk.activities.common.models is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.common.models instead.",
    DeprecationWarning,
    stacklevel=2,
)
from application_sdk.common.models import (  # noqa: E402, F401
    ActivityResult,
    ActivityStatistics,
)

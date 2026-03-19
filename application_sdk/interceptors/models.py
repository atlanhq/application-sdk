"""Deprecated: use application_sdk.contracts.events instead."""

import warnings

warnings.warn(
    "application_sdk.interceptors.models is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.contracts.events instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.contracts.events import *  # noqa: E402, F401, F403
from application_sdk.contracts.events import (  # noqa: E402, F401
    ApplicationEventNames,
    Consumes,
    Event,
    EventFilter,
    EventMetadata,
    EventRegistration,
    EventTypes,
    WorkerStartEventData,
    WorkerTokenRefreshEventData,
    WorkflowStates,
)

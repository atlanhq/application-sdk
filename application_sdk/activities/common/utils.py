"""Deprecated: use application_sdk.execution._temporal.activity_utils instead."""

import warnings

warnings.warn(
    "application_sdk.activities.common.utils is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.execution._temporal.activity_utils instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.execution._temporal.activity_utils import *  # noqa: E402, F401, F403
from application_sdk.execution._temporal.activity_utils import (  # noqa: E402, F401
    _get_heartbeat_timeout,
    auto_heartbeater,
    build_output_path,
    get_object_store_prefix,
    get_workflow_id,
    get_workflow_run_id,
    send_periodic_heartbeat,
    send_periodic_heartbeat_sync,
)

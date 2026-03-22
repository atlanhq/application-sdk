"""SDR (Self Deployed Runtime) activities for the application SDK.

These activities provide per-operation implementations of the three SDR operations
(test_auth, preflight, fetch_metadata) that every connector must support.
They are automatically registered on every worker by BaseApplication.setup_workflow()
unless explicitly disabled via enable_sdr=False.
"""

from application_sdk.activities.sdr.fetch_metadata import FetchMetadataActivities
from application_sdk.activities.sdr.preflight import PreflightCheckActivities
from application_sdk.activities.sdr.test_auth import TestAuthActivities

__all__ = [
    "TestAuthActivities",
    "PreflightCheckActivities",
    "FetchMetadataActivities",
]

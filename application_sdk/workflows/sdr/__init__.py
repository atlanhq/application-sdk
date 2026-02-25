"""SDR (Self Deployed Runtime) workflows for the application SDK.

These three workflows are automatically registered on every worker by
BaseApplication.setup_workflow() unless explicitly disabled via enable_sdr=False.
"""

from application_sdk.workflows.sdr.fetch_metadata import FetchMetadataWorkflow
from application_sdk.workflows.sdr.preflight import PreflightCheckWorkflow
from application_sdk.workflows.sdr.test_auth import TestAuthWorkflow

__all__ = [
    "TestAuthWorkflow",
    "PreflightCheckWorkflow",
    "FetchMetadataWorkflow",
]

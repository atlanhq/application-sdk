"""End-to-end test infrastructure for application_sdk apps.

Two sub-surfaces live here:

K8s utilities (existing)::

    from application_sdk.testing.harness import AppUnderTest
    from application_sdk.testing.e2e import (
        LogCollector,
        kube_http_call,
        run_workflow,
        wait_for_workflow,
    )

Full-DAG AE harness (new — subclass per connector)::

    from application_sdk.testing.e2e import BaseE2ETest, RunMode
    from application_sdk.testing.e2e import DAGSpec  # N DAG runs in one suite
    from application_sdk.testing.e2e import SQLAppE2ETest          # SQL connectors
    from application_sdk.testing.e2e.payload import AgentSpec, ConnectionSpec
    from application_sdk.testing.e2e.client import AEWorkflowClient
"""

from application_sdk.testing.e2e.base import (
    BaseE2ETest,
    DAGSpec,
    FullDAGOutcome,
    ResolvedDAG,
)
from application_sdk.testing.e2e.config import AppConfig
from application_sdk.testing.e2e.logs import LogCollector
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.portforward import kube_http_call
from application_sdk.testing.e2e.sql_app import SQLAppE2ETest
from application_sdk.testing.e2e.workflows import run_workflow, wait_for_workflow

__all__ = [
    # K8s utilities
    # Deprecated (removed in v4.0) — use
    # application_sdk.testing.harness.AppUnderTest
    "AppConfig",
    "LogCollector",
    "kube_http_call",
    "run_workflow",
    "wait_for_workflow",
    # Full-DAG AE harness
    "BaseE2ETest",
    "DAGSpec",
    "FullDAGOutcome",
    "ResolvedDAG",
    "RunMode",
    "SQLAppE2ETest",
]

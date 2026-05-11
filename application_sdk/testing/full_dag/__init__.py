"""Shared harness for tier-4 / tier-5 full-DAG end-to-end tests.

Tier 4 (e2e-sdr-full) and tier 5 (e2e-full) both validate connectors
end-to-end against the *tenant's* system apps (publish, query-intelligence,
lineage, lineage-publish) on top of the connector's own extract step. The
moving parts are identical:

1. Submit an Automation Engine workflow via
   ``POST /api/service/package-workflows?submit=true``.
2. Poll the run via
   ``GET /api/service/package-workflows/native-status/<run_id>``
   until every DAG node reaches a terminal status.
3. Poll Atlas via
   ``GET /api/meta/entity/uniqueAttribute/type/Connection?attr:qualifiedName=...``
   until the resulting Connection asset shows up.

The only thing that differs between tier 4 and tier 5 is *where the
connector image runs* — tier 4 uses a CI-side docker compose worker
registered on a unique Temporal queue (using the agent flow); tier 5
uses a tenant-deployed pod (using the direct flow). The submission +
polling + assertion are the same.

Public surface:

- :class:`BaseFullDAGE2ETest` — pytest base class subclasses configure
  with connector-specific knobs (app name, Argo template, connection
  qualifying-name prefix) and then call :meth:`run_full_dag` to execute.

- :func:`build_ae_payload` — payload builder shared between the agent
  (tier 4) and direct (tier 5) modes.

- :class:`AEWorkflowClient` — thin HTTP wrapper around the three Atlan
  endpoints used above, with retry on transient 5xx and a structured
  return shape per DAG node.

See ``application_sdk.testing.sdr`` for the connector-only validation
that runs in tier 3 (BLDX-1254). Tier 4/5 complement tier 3 by adding
the tenant-side system-apps validation.
"""

from application_sdk.testing.full_dag.base import BaseFullDAGE2ETest, RunMode
from application_sdk.testing.full_dag.client import (
    AEWorkflowClient,
    DAGNodeStatus,
    DAGRunStatus,
)
from application_sdk.testing.full_dag.payload import build_ae_payload

__all__ = [
    "AEWorkflowClient",
    "BaseFullDAGE2ETest",
    "DAGNodeStatus",
    "DAGRunStatus",
    "RunMode",
    "build_ae_payload",
]

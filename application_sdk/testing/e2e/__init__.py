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
    from application_sdk.testing.e2e import SystemAppE2ETest       # system apps
    from application_sdk.testing.e2e.payload import AgentSpec, ConnectionSpec

The Automation Engine run types a suite grades (``DAGRunResult``,
``DAGNodeStatus`` and the rest) live in the harness::

    from application_sdk.testing.harness.automation_engine import DAGRunResult

Asserting on attribute VALUES needs the matcher vocabulary, which lives with the
evaluator that grades it::

    from application_sdk.testing.harness.expectations import (
        Absent, AtLeast, AtMost, Exactly, Present,
    )

``expected_asset_attributes`` carries the claim every asset of a type shares;
``expected_asset_attributes_at`` (type -> qualifiedName suffix -> attribute)
pins a value that differs between assets of the same type, which no per-type
claim can express.
"""

from typing import TYPE_CHECKING, Any

from application_sdk._install import lazy_getattr

if TYPE_CHECKING:
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
    from application_sdk.testing.e2e.system_app import (
        FieldCondition,
        FieldOp,
        RecordFormat,
        StoreAbsent,
        StoreCount,
        StoreExpectation,
        StorePresent,
        StoreRecords,
        SystemAppE2ETest,
    )
    from application_sdk.testing.e2e.tenant_pool import TenantPool
    from application_sdk.testing.e2e.workflows import (
        run_workflow,
        wait_for_workflow,
    )

#: Every public name, served on first access (PEP 562). ``base`` alone loads
#: pyatlan, obstore and Temporal; a generated ``CredentialBody`` import only
#: needs ``e2e.credential`` (pydantic), so the package must not load them for
#: it (FND-4658).
_LAZY: dict[str, tuple[str, str]] = {
    "BaseE2ETest": ("application_sdk.testing.e2e.base", "BaseE2ETest"),
    "DAGSpec": ("application_sdk.testing.e2e.base", "DAGSpec"),
    "FullDAGOutcome": ("application_sdk.testing.e2e.base", "FullDAGOutcome"),
    "ResolvedDAG": ("application_sdk.testing.e2e.base", "ResolvedDAG"),
    "AppConfig": ("application_sdk.testing.e2e.config", "AppConfig"),
    "LogCollector": ("application_sdk.testing.e2e.logs", "LogCollector"),
    "RunMode": ("application_sdk.testing.e2e.payload", "RunMode"),
    "kube_http_call": ("application_sdk.testing.e2e.portforward", "kube_http_call"),
    "SQLAppE2ETest": ("application_sdk.testing.e2e.sql_app", "SQLAppE2ETest"),
    "FieldCondition": ("application_sdk.testing.e2e.system_app", "FieldCondition"),
    "FieldOp": ("application_sdk.testing.e2e.system_app", "FieldOp"),
    "RecordFormat": ("application_sdk.testing.e2e.system_app", "RecordFormat"),
    "StoreAbsent": ("application_sdk.testing.e2e.system_app", "StoreAbsent"),
    "StoreCount": ("application_sdk.testing.e2e.system_app", "StoreCount"),
    "StoreExpectation": ("application_sdk.testing.e2e.system_app", "StoreExpectation"),
    "StorePresent": ("application_sdk.testing.e2e.system_app", "StorePresent"),
    "StoreRecords": ("application_sdk.testing.e2e.system_app", "StoreRecords"),
    "SystemAppE2ETest": ("application_sdk.testing.e2e.system_app", "SystemAppE2ETest"),
    "TenantPool": ("application_sdk.testing.e2e.tenant_pool", "TenantPool"),
    "run_workflow": ("application_sdk.testing.e2e.workflows", "run_workflow"),
    "wait_for_workflow": ("application_sdk.testing.e2e.workflows", "wait_for_workflow"),
}

#: Submodules this package imported eagerly before it went lazy, so
#: ``import pkg; pkg.<submodule>`` keeps resolving them.
_EAGER_SUBMODULES: tuple[str, ...] = (
    "base",
    "config",
    "logs",
    "payload",
    "portforward",
    "sql_app",
    "system_app",
    "tenant_pool",
    "workflows",
)

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
    "FieldCondition",
    "FieldOp",
    "RecordFormat",
    "StoreAbsent",
    "StoreCount",
    "StoreExpectation",
    "StorePresent",
    "StoreRecords",
    "SystemAppE2ETest",
    "TenantPool",
]


def __getattr__(name: str) -> Any:
    return lazy_getattr(__name__, name, _LAZY, globals(), _EAGER_SUBMODULES)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | set(_EAGER_SUBMODULES))

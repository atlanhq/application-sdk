"""Deprecated: the harness's async pytest fixtures and their evidence plugin.

The fixtures had no consumer outside this repository's own tests: no connector
registers this plugin or composes these fixtures (FND-4659). The implementation
now lives in :mod:`application_sdk.testing.harness._fixtures`; every name here
still resolves to the same object, with a ``DeprecationWarning`` on access, and
``pytest_plugins = ["application_sdk.testing.harness.fixtures"]`` still loads
them (``__dir__`` lists every name, so pytest and pluggy find each fixture and
the report hook).

.. deprecated:: 3.56
    Drive the harness through :class:`~application_sdk.testing.e2e.BaseE2ETest`,
    or compose :mod:`application_sdk.testing.harness` directly from synchronous
    code with :func:`~application_sdk.testing.harness.run_sync`. Will be removed
    in v4.0.0.
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing.harness._fixtures import (  # noqa: F401
        DEFAULT_EVIDENCE_DIR,
        EvidenceLog,
        harness_ae_client,
        harness_app_under_test,
        harness_atlas_client,
        harness_budget_profile,
        harness_cluster_reader,
        harness_connection_identity,
        harness_connection_teardown,
        harness_connection_type,
        harness_environ,
        harness_evidence,
        harness_evidence_dir,
        harness_kube_context,
        harness_minter,
        harness_precondition_checks,
        harness_preconditions,
        harness_run_id,
        harness_substrate,
        harness_sync_bridge,
        harness_tenant_auth,
        harness_worker_health_url,
        logger,
        pytest_runtest_makereport,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the implementation's object itself, so a patch or an ``isinstance``
#: check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "DEFAULT_EVIDENCE_DIR": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "EvidenceLog": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "logger": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "pytest_runtest_makereport": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_ae_client": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_app_under_test": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_atlas_client": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_budget_profile": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_cluster_reader": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_connection_identity": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_connection_type": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_connection_teardown": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_environ": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_evidence": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_evidence_dir": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_kube_context": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_minter": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_precondition_checks": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_preconditions": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_run_id": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_substrate": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_sync_bridge": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_tenant_auth": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
    "harness_worker_health_url": (
        "application_sdk.testing.e2e.BaseE2ETest, or application_sdk.testing.harness composed through application_sdk.testing.harness.run_sync",
        "the harness fixture plugin has no consumer and leaves the package (FND-4659)",
    ),
}

_LAZY: dict[str, tuple[str, str]] = {
    "DEFAULT_EVIDENCE_DIR": (
        "application_sdk.testing.harness._fixtures",
        "DEFAULT_EVIDENCE_DIR",
    ),
    "EvidenceLog": ("application_sdk.testing.harness._fixtures", "EvidenceLog"),
    "logger": ("application_sdk.testing.harness._fixtures", "logger"),
    "pytest_runtest_makereport": (
        "application_sdk.testing.harness._fixtures",
        "pytest_runtest_makereport",
    ),
    "harness_ae_client": (
        "application_sdk.testing.harness._fixtures",
        "harness_ae_client",
    ),
    "harness_app_under_test": (
        "application_sdk.testing.harness._fixtures",
        "harness_app_under_test",
    ),
    "harness_atlas_client": (
        "application_sdk.testing.harness._fixtures",
        "harness_atlas_client",
    ),
    "harness_budget_profile": (
        "application_sdk.testing.harness._fixtures",
        "harness_budget_profile",
    ),
    "harness_cluster_reader": (
        "application_sdk.testing.harness._fixtures",
        "harness_cluster_reader",
    ),
    "harness_connection_identity": (
        "application_sdk.testing.harness._fixtures",
        "harness_connection_identity",
    ),
    "harness_connection_type": (
        "application_sdk.testing.harness._fixtures",
        "harness_connection_type",
    ),
    "harness_connection_teardown": (
        "application_sdk.testing.harness._fixtures",
        "harness_connection_teardown",
    ),
    "harness_environ": ("application_sdk.testing.harness._fixtures", "harness_environ"),
    "harness_evidence": (
        "application_sdk.testing.harness._fixtures",
        "harness_evidence",
    ),
    "harness_evidence_dir": (
        "application_sdk.testing.harness._fixtures",
        "harness_evidence_dir",
    ),
    "harness_kube_context": (
        "application_sdk.testing.harness._fixtures",
        "harness_kube_context",
    ),
    "harness_minter": ("application_sdk.testing.harness._fixtures", "harness_minter"),
    "harness_precondition_checks": (
        "application_sdk.testing.harness._fixtures",
        "harness_precondition_checks",
    ),
    "harness_preconditions": (
        "application_sdk.testing.harness._fixtures",
        "harness_preconditions",
    ),
    "harness_run_id": ("application_sdk.testing.harness._fixtures", "harness_run_id"),
    "harness_substrate": (
        "application_sdk.testing.harness._fixtures",
        "harness_substrate",
    ),
    "harness_sync_bridge": (
        "application_sdk.testing.harness._fixtures",
        "harness_sync_bridge",
    ),
    "harness_tenant_auth": (
        "application_sdk.testing.harness._fixtures",
        "harness_tenant_auth",
    ),
    "harness_worker_health_url": (
        "application_sdk.testing.harness._fixtures",
        "harness_worker_health_url",
    ),
}

#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. A literal, because the symbol gate reads it statically.
__all__ = [
    "DEFAULT_EVIDENCE_DIR",
    "EvidenceLog",
    "logger",
    "pytest_runtest_makereport",
    "harness_ae_client",
    "harness_app_under_test",
    "harness_atlas_client",
    "harness_budget_profile",
    "harness_cluster_reader",
    "harness_connection_identity",
    "harness_connection_type",
    "harness_connection_teardown",
    "harness_environ",
    "harness_evidence",
    "harness_evidence_dir",
    "harness_kube_context",
    "harness_minter",
    "harness_precondition_checks",
    "harness_preconditions",
    "harness_run_id",
    "harness_substrate",
    "harness_sync_bridge",
    "harness_tenant_auth",
    "harness_worker_health_url",
]


def __getattr__(name: str) -> object:
    """Serve the deprecated names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module, attr = _LAZY[name]
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return sorted({*globals(), *_DEPRECATED_CONSTANTS})

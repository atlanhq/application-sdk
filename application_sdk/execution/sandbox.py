"""Sandbox configuration for Temporal workflow execution.

Abstracts Temporal's SandboxRestrictions API to simplify specifying
which modules should pass through the sandbox without re-import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.worker.workflow_sandbox import SandboxRestrictions


# Framework modules that should always pass through the sandbox
_FRAMEWORK_MODULES: frozenset[str] = frozenset(
    {
        "application_sdk",
        "application_sdk.app",
        "application_sdk.app.base",
        "application_sdk.app.task",
        "application_sdk.app.context",
        "application_sdk.app.registry",
        "application_sdk.contracts",
        "application_sdk.contracts.base",
        "application_sdk.contracts.types",
        "application_sdk.execution",
        "application_sdk.execution._temporal",
        "application_sdk.execution._temporal.workflows",
        "application_sdk.execution._temporal.activities",
        "application_sdk.execution._temporal.backend",
        "application_sdk.execution._temporal.interceptors",
        "application_sdk.execution._temporal.interceptors.events",
        "application_sdk.execution._temporal.interceptors.event_bus",
        "application_sdk.execution._temporal.interceptors.event_interceptor",
        "application_sdk.execution._temporal.interceptors.correlation_interceptor",
        "application_sdk.observability",
        "application_sdk.observability.correlation",
        # OpenTelemetry accesses os.environ at import time, which the sandbox blocks.
        "opentelemetry",
        # pyatlan Struct types must use real module objects in the sandbox.
        "pyatlan",
        "pyatlan_v9",
    }
)


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for workflow sandbox behavior.

    Framework modules are always passed through automatically.
    Use ``with_passthrough_modules()`` to add app-specific modules.

    Example:
        class MyApp(App):
            passthrough_modules = {"pandas", "numpy"}

        worker = create_worker(client, passthrough_modules={"shared_utils"})
    """

    passthrough_modules: frozenset[str] = field(default_factory=frozenset)

    def with_passthrough_modules(self, *modules: str) -> SandboxConfig:
        """Return new config with additional passthrough modules.

        Args:
            modules: Module names to add to the passthrough set.

        Returns:
            New SandboxConfig with the additional modules.
        """
        return SandboxConfig(
            passthrough_modules=self.passthrough_modules | frozenset(modules)
        )

    def to_temporal_restrictions(self) -> SandboxRestrictions:
        """Convert to Temporal's SandboxRestrictions.

        Returns:
            SandboxRestrictions with framework + user-specified passthrough modules.
        """
        from temporalio.worker.workflow_sandbox import SandboxRestrictions

        all_modules = _FRAMEWORK_MODULES | self.passthrough_modules
        return SandboxRestrictions.default.with_passthrough_modules(*all_modules)

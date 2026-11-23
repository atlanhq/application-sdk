"""Sandbox configuration for Temporal workflow execution.

Abstracts Temporal's SandboxRestrictions API to simplify specifying
which modules should pass through the sandbox without re-import.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.worker.workflow_sandbox import SandboxRestrictions


# Every top-level module and subpackage of application_sdk, classified. The
# sandbox passes a module through by name prefix, so one "application_sdk"
# entry would pass through the whole package, the templates included, and
# none of their workflow code would ever be checked.
#
# Passed through: SDK internals that must exist once per process. The
# registries, the per-run state, the error classes matched by isinstance, and
# the generated workflow's own runtime live here, and host SDK code imports
# from them lazily while a workflow runs.
_SDK_PASSTHROUGH_PACKAGES: frozenset[str] = frozenset(
    {
        "_build_info",
        "_context_errors",
        "_deprecation",
        "_discovery_errors",
        "_env",
        "_install",
        "_logging",
        "_loop_stall_watchdog",
        "_obstore_config",
        "_runtime",
        "app",
        "clients",
        "common",
        "constants",
        "contracts",
        "credentials",
        "dev",
        "discovery",
        "errors",
        "execution",
        "handler",
        "infrastructure",
        "main",
        "main_errors",
        "observability",
        "outputs",
        "pkl_version",
        "server",
        "storage",
        "testing",
        "tools",
        "transformers",
        "validation",
        "version",
    }
)

# Re-imported in the sandbox for every run, like an app's own modules: the
# templates hold run() bodies that apps inherit, which is SDK workflow code.
_SDK_SANDBOXED_PACKAGES: frozenset[str] = frozenset({"templates"})

# Framework modules that should always pass through the sandbox
_FRAMEWORK_MODULES: frozenset[str] = frozenset(
    {f"application_sdk.{name}" for name in _SDK_PASSTHROUGH_PACKAGES}
    | {
        # OpenTelemetry accesses os.environ at import time, which the sandbox blocks.
        "opentelemetry",
        # beartype.claw installs a global import hook; passing it through prevents
        # the sandbox importer from intercepting beartype's own internal imports
        # and causing infinite recursion when any dependency uses beartype_this_package().
        "beartype",
        # pyatlan Struct types must use real module objects in the sandbox.
        "pyatlan",
        "pyatlan_v9",
    }
)

# Names in sys.modules that are never a library to pass through.
_NOT_LIBRARIES: frozenset[str] = frozenset(
    {"__main__", "__temporal_main__", "application_sdk"}
)


def host_library_modules(sandboxed_packages: Iterable[str]) -> frozenset[str]:
    """Top-level packages already imported in this process, as passthrough names.

    Temporal re-imports every module that is not passed through, once per
    workflow run. That is what checks app code, but re-importing a third-party
    library adds no check worth having (its code runs with host globals once
    called) and many libraries cannot be re-imported at all: a compiled
    extension such as ``msgspec`` fails with a ``KeyError`` on its own
    submodule. By the time a worker is built the host has imported every app
    module and, with them, every library they import, so passing those
    libraries through leaves exactly the app's own package to the sandbox.

    Args:
        sandboxed_packages: Top-level package names to leave out, so that the
            sandbox re-imports and checks them: the registered apps' packages.

    Returns:
        The top-level name of every imported module, minus
        ``sandboxed_packages`` and ``application_sdk`` (whose passthrough is
        the explicit, per-subpackage ``_FRAMEWORK_MODULES``).
    """
    excluded = _NOT_LIBRARIES | frozenset(sandboxed_packages)
    # list(): another thread importing while this runs would otherwise change
    # the dict's size mid-iteration.
    tops = {name.partition(".")[0] for name in list(sys.modules)}
    return frozenset(tops - excluded)


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
        from temporalio.worker.workflow_sandbox import (  # noqa: PLC0415 — cold path: sandbox restrictions only at worker startup
            SandboxRestrictions,
        )

        all_modules = _FRAMEWORK_MODULES | self.passthrough_modules
        restrictions = SandboxRestrictions.default.with_passthrough_modules(
            *all_modules
        )
        return _allow_os_path(restrictions)


def _allow_os_path(restrictions: SandboxRestrictions) -> SandboxRestrictions:
    """Allow ``os.path`` in workflow code on every Python version.

    temporalio restricts ``os.path`` on Python < 3.14 and allows it on 3.14+
    (CPython needs it there to render tracebacks). Left alone, workflow code
    that builds a path with ``os.path.join`` passes on a 3.14 laptop and fails
    its workflow task on the 3.13 production image. This applies the 3.14
    rule everywhere, so one app behaves the same on both.
    """
    import dataclasses  # noqa: PLC0415 — cold path: worker startup only

    from temporalio.worker.workflow_sandbox import (  # noqa: PLC0415 — cold path: sandbox restrictions only at worker startup
        SandboxMatcher,
    )

    members = restrictions.invalid_module_members
    os_matcher = members.children.get("os")
    if os_matcher is None:
        return restrictions
    os_matcher = dataclasses.replace(
        os_matcher,
        children={**os_matcher.children, "path": SandboxMatcher.none},
        exclude=frozenset(os_matcher.exclude) | {"path"},
    )
    members = dataclasses.replace(
        members, children={**members.children, "os": os_matcher}
    )
    return dataclasses.replace(restrictions, invalid_module_members=members)

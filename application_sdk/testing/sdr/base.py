"""Base class for Self-Deployed Runtime (SDR) integration tests.

Connector apps subclass :class:`BaseSDRIntegrationTest` to run their auth /
preflight / workflow scenarios against a real SDR container — the same
compose stack atlan-configurator generates for customer deployments,
running entirely on the GitHub Actions runner.

What this base class adds over :class:`BaseIntegrationTest`:

1. **Workflow completion polling.** The base integration runner only polls
   ``GET /workflows/v1/status/{wf}/{run}`` when a scenario sets
   ``expected_data`` or ``schema_base_path``. SDR scenarios commonly want
   completion polling without metadata validation, so this class always
   polls workflow scenarios that set a non-zero ``workflow_timeout`` —
   ``FAILED`` / ``TIMEOUT`` statuses surface as test failures instead of
   zombies on the tenant's Temporal UI.

2. **Agent credential routing.** Workflow scenarios are auto-injected with
   ``extraction_method="agent"`` + ``agent_json=cls.agent_spec_template``
   so the SDK's :class:`CredentialRef.resolve` routes through the Dapr
   ``local.file`` secret store at ``/app/secrets/credentials.json``.
   Subclasses just declare their agent spec template; the routing is
   handled here.

3. **Multi-entrypoint support.** Connectors with multiple entrypoints
   (e.g. variants behind a single app module) can set ``workflow_type``
   on the subclass to inject the entrypoint short name into workflow
   start args.

Example:

    class TestMyConnectorSDR(BaseSDRIntegrationTest):
        agent_spec_template = {
            "agent-name": "myconn-ci-agent",
            "secret-manager": "local",
            "secret-path": "myconn-credentials",
            "auth-type": "basic",
            "host": _host,
            "port": _port,
            "basic.username": "username",
            "basic.password": "password",
        }
        scenarios = [
            Scenario(name="auth_valid", api="auth", ...),
            Scenario(name="workflow_runs", api="workflow", workflow_timeout=300, ...),
        ]
"""

from __future__ import annotations

import os
import shutil
from typing import Any, ClassVar, Dict, Optional

from application_sdk.testing.integration import (
    BaseIntegrationTest,
    Scenario,
    ScenarioResult,
)


class BaseSDRIntegrationTest(BaseIntegrationTest):
    """Base class for SDR integration tests.

    Class attributes subclasses are expected to set:

    Attributes:
        agent_spec_template: Dict matching :class:`AgentCredentialSpec` for
            the connector's secret-store-resolved credentials. Injected
            into workflow scenario args as ``agent_json``. Empty dict
            disables agent routing (auth / preflight scenarios still work
            via inline credentials).
        workflow_type: For connectors with multiple entrypoints, the
            entrypoint short name (e.g. ``"ecc"``, ``"s4"``). Injected
            as ``workflow_type`` on workflow scenarios. ``None`` for
            single-entrypoint apps.
    """

    agent_spec_template: ClassVar[Dict[str, Any]] = {}
    workflow_type: ClassVar[Optional[str]] = None

    def _build_scenario_args(self, scenario: Scenario) -> Dict[str, Any]:
        args = super()._build_scenario_args(scenario)
        if scenario.api.lower() == "workflow" and self.agent_spec_template:
            args["extraction_method"] = "agent"
            args["agent_json"] = self.agent_spec_template
            if self.workflow_type:
                args["workflow_type"] = self.workflow_type
        return args

    def _execute_scenario(self, scenario: Scenario) -> ScenarioResult:
        result = super()._execute_scenario(scenario)
        # The base class only polls when expected_data or schema_base_path
        # is set. SDR workflow scenarios usually want completion polling
        # without metadata validation, so always poll when workflow_timeout
        # > 0 — surfaces FAILED/TIMEOUT as test failures.
        if (
            scenario.api.lower() == "workflow"
            and scenario.workflow_timeout > 0
            and not scenario.expected_data
            and not (scenario.schema_base_path or self.schema_base_path)
            and result.success
            and result.response
        ):
            try:
                self._ensure_workflow_completed(scenario, result.response)
            except Exception as exc:
                # The parent's try/except/finally already appended `result`
                # to cls._results with success=True. Mutate the same object
                # so the on-disk summary reflects the actual outcome —
                # otherwise the post-run report shows ✅ on a scenario that
                # pytest reported as FAILED.
                result.success = False
                result.error = exc
                raise
        return result

    @classmethod
    def _write_summary(cls) -> Optional[str]:
        """Multi-class-safe variant of :meth:`BaseIntegrationTest._write_summary`.

        The parent writes the run summary to a fixed path
        (``./integration-test-summary.json`` by default). For multi-class
        test files (e.g. saperp's ``TestSAPERPSdrECC`` + ``TestSAPERPSdrS4``)
        each class's ``teardown_class`` overwrites the previous one, so the
        on-disk file ends up containing only the last class's scenarios.

        We let the parent write the shared file as-is, then copy the result
        to a per-class file (``integration-test-summary-<ClassName>.json``).
        Downstream consumers (e.g. the Temporal-link extractor in the SDR
        composite action) glob the per-class files when present.
        """
        written = super()._write_summary()
        if not written:
            return None
        base, ext = os.path.splitext(written)
        per_class_path = f"{base}-{cls.__name__}{ext}"
        try:
            shutil.copyfile(written, per_class_path)
        except OSError:
            # Best-effort: the shared file was already written; per-class
            # copy failure shouldn't fail the test session.
            pass
        return written

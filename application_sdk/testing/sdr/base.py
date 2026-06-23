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
from pathlib import Path
from typing import Any, ClassVar

import orjson

from application_sdk.testing.integration import (
    BaseIntegrationTest,
    Scenario,
    ScenarioResult,
)


def _apply_mustache_subs(obj: Any, subs: dict[str, Any]) -> Any:
    """Recursively replace exact-match ``{{...}}`` strings with ``subs`` values.

    Mirrors the walker the e2e / full_dag manifest harnesses use
    (``application_sdk.testing.e2e.base._apply_mustache_subs``); kept local so
    the SDR harness can substitute a manifest's ``extract`` args without
    depending on the AE-publish base class.
    """
    if isinstance(obj, dict):
        return {k: _apply_mustache_subs(v, subs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_apply_mustache_subs(x, subs) for x in obj]
    if isinstance(obj, str) and obj in subs:
        return subs[obj]
    return obj


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

    agent_spec_template: ClassVar[dict[str, Any]] = {}
    workflow_type: ClassVar[str | None] = None

    #: Path to the connector's generated ``manifest.json`` (relative to the
    #: test's cwd, or absolute). When set, workflow-scenario input is built
    #: from the manifest's ``dag.extract.inputs.args`` — the SAME shape the
    #: platform (Heracles/AE) submits in production — instead of the
    #: hand-written ``agent_spec_template`` injection. This is what lets the
    #: SDR e2e catch a manifest that fails to wire a field into the workflow
    #: input (e.g. a missing ``agent_json`` slot — atlan-mssql-app#177): the
    #: derived input is then faithfully missing that field and the agent run
    #: fails, instead of the test silently passing with a hand-supplied value.
    #: Empty string ("") keeps the legacy hand-written behaviour.
    manifest_path: ClassVar[str] = ""

    #: Table include/exclude filters substituted into the manifest's
    #: ``{{include-filter}}`` / ``{{exclude-filter}}`` placeholders (manifest
    #: path only). Default empty — extract everything.
    include_filter: ClassVar[str] = ""
    exclude_filter: ClassVar[str] = ""

    def _build_scenario_args(self, scenario: Scenario) -> dict[str, Any]:
        args = super()._build_scenario_args(scenario)
        if scenario.api.lower() != "workflow":
            return args
        if self.manifest_path:
            return self._workflow_args_from_manifest(args)
        # Legacy hand-written path (no manifest declared): inject agent routing
        # unconditionally from the template.
        if self.agent_spec_template:
            args["extraction_method"] = "agent"
            args["agent_json"] = self.agent_spec_template
            if self.workflow_type:
                args["workflow_type"] = self.workflow_type
        return args

    def _manifest_extract_inputs(self) -> tuple[dict[str, Any], str | None]:
        """Load the connector's manifest and return its extract node's
        ``(args, workflow_type)``.

        Raises if the manifest is missing or has no ``dag.extract.inputs.args``
        — a manifest-driven SDR test with no usable extract node is a config
        error, not something to silently fall back on.
        """
        path = Path(self.manifest_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise FileNotFoundError(
                f"SDR manifest not found at {path} — set `manifest_path` to the "
                "connector's manifest.json, or '' to use agent_spec_template."
            )
        manifest = orjson.loads(path.read_bytes())
        inputs = ((manifest.get("dag") or {}).get("extract") or {}).get("inputs") or {}
        if not isinstance(inputs.get("args"), dict):
            raise ValueError(
                f"Manifest at {path} has no `dag.extract.inputs.args` object — "
                "cannot derive the workflow input from it."
            )
        return inputs["args"], inputs.get("workflow_type")

    def _workflow_args_from_manifest(self, base_args: dict[str, Any]) -> dict[str, Any]:
        """Build workflow input from the manifest's extract args + substitutions.

        The substitution keys/literals mirror
        :class:`application_sdk.testing.e2e.substitutions.SQLMustacheSubstitutions`
        (the e2e harness's canonical map); built directly here so the connection
        dict and agent spec the SDR test already holds pass straight through.
        """
        extract_args, manifest_wf_type = self._manifest_extract_inputs()
        method = "agent" if self.agent_spec_template else "direct"
        subs: dict[str, Any] = {
            "{{credential}}": None,
            "{{credential-guid}}": "",
            "{{connection}}": base_args.get("connection") or {},
            "{{extraction-method}}": method,
            "{{agent-json}}": self.agent_spec_template or None,
            "{{include-filter}}": self.include_filter,
            "{{exclude-filter}}": self.exclude_filter,
            "{{exclude-table-regex}}": "",
            "{{temp-table-regex}}": "",
            "{{preflight-check}}": True,
        }
        wf_args = _apply_mustache_subs(extract_args, subs)
        # Carry credentials/metadata through for the start endpoint: agent mode
        # resolves the DB credential via agent_json, but direct mode and the
        # client's credential provisioning still rely on these.
        merged: dict[str, Any] = {
            k: base_args[k] for k in ("credentials", "metadata") if k in base_args
        }
        merged.update(wf_args)
        wf_type = self.workflow_type or manifest_wf_type
        if wf_type:
            merged["workflow_type"] = wf_type
        return merged

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
            # conformance: ignore[E004] re-raises immediately; only mutates result object before propagation so caller boundary handles logging
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
    def _write_summary(cls) -> str | None:
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
        except OSError:  # conformance: ignore[E002] best-effort per-class copy; shared summary already written
            pass
        return written

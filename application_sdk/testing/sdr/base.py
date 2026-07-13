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
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing._mustache import PLACEHOLDER_RE as _PLACEHOLDER_RE
from application_sdk.testing._mustache import (
    apply_mustache_subs as _apply_mustache_subs,
)
from application_sdk.testing.integration import (
    BaseIntegrationTest,
    Scenario,
    ScenarioResult,
)

logger = get_logger(__name__)


def _collect_placeholders(obj: Any) -> set[str]:
    """Every exact-match ``{{...}}`` string anywhere in ``obj``.

    Used to default any manifest placeholder no substitution covers to "",
    so an unset connector-specific slot never leaks a literal ``{{...}}``.
    """
    found: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            found |= _collect_placeholders(v)
    elif isinstance(obj, list):
        for x in obj:
            found |= _collect_placeholders(x)
    elif isinstance(obj, str) and _PLACEHOLDER_RE.fullmatch(obj):
        found.add(obj)
    return found


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

    #: When True, a workflow scenario that runs to completion must have written
    #: NON-EMPTY extracted output at ``extracted_output_base_path`` — catching the
    #: "COMPLETED with status success but zero assets" silent SDR failure (and,
    #: because the output is read from the exact ``{base}/{workflow_id}/{run_id}``
    #: path, an egress-path bug that writes to the wrong prefix).
    #:
    #: Default False (OPT-IN): reading the output requires the connector's SDR
    #: object store to be bind-mounted to the host at ``extracted_output_base_path``.
    #: The current CI SDR stack does NOT mount it — the deployment localstorage
    #: (``bindings.localstorage`` rootPath ``/tmp/atlan-data``) lives inside the
    #: container, so the host path is empty. Enabling this without that mount would
    #: false-fail. Flip on per connector once the deployment store is mounted to the
    #: host (see the two-store CI stack work). Skipped with a warning when no
    #: ``extracted_output_base_path`` is set.
    require_assets_landed: ClassVar[bool] = False

    #: When True, EVERY landed asset's ``attributes.qualifiedName`` must be nested
    #: under the connection's qualifiedName prefix — catching assets that landed
    #: with a wrong/missing connection prefix (mis-parented / dropped prefix) even
    #: when the count is right. Skipped (with a warning) when the connection QN
    #: can't be resolved from ``default_connection``. Default True; set False to
    #: opt out for a connector whose extracted QNs legitimately aren't
    #: connection-prefixed.
    require_asset_connection_prefix: ClassVar[bool] = True

    #: Optional per-typeName minimum asset counts, e.g.
    #: ``{"Database": 1, "Schema": 1, "Table": 3, "Column": 10}``. When set,
    #: :meth:`_assert_assets_landed` additionally asserts each type meets its floor.
    #: This closes SDR-QA gap item 7: partial / mis-typed extraction can land SOME
    #: assets — passing the non-zero + location guards — while silently missing a
    #: whole type (e.g. Columns never extracted). The non-zero guard cannot see
    #: that; a per-type floor can. Empty (default) = only non-zero + location run,
    #: so existing suites are unaffected until they opt in.
    expected_min_asset_counts: ClassVar[dict[str, int]] = {}

    #: When True, an agent-mode SDR suite that declares NO ``api="workflow"``
    #: scenario fails the readiness floor (it never validates a real extraction).
    #: Default False: the floor reports the gap as a skipped test rather than a
    #: fleet-wide red. Flip on once the suite has a workflow-to-completion scenario.
    enforce_workflow_floor: ClassVar[bool] = False

    #: When True, additionally assert the UPSTREAM (atlan) object store's
    #: ``transformed/`` is populated after the extraction — the store the
    #: atlan-side Publish app actually reads. In SDR (two-store) mode the
    #: connector must copy ``transformed/`` from the deployment store to the
    #: upstream store (``upload_to_atlan`` / ``ENABLE_ATLAN_UPLOAD``). If it
    #: doesn't, extraction is green and the deployment store has assets, but
    #: Publish finds an empty ``transformed/`` and publishes ZERO assets — the
    #: atlan-looker-app#134 class of bug, which the deployment-store check above
    #: cannot see. Default False: needs a two-store CI stack + a set
    #: ``upstream_output_base_path``; flip on per connector once it wires the
    #: upstream upload.
    require_upstream_assets_landed: ClassVar[bool] = False

    #: Local path where the UPSTREAM (atlan) object store's output is mounted in
    #: the two-store CI SDR stack (e.g. ``data-upstream/artifacts/apps/<app>/workflows``).
    #: Required for :pyattr:`require_upstream_assets_landed`.
    upstream_output_base_path: ClassVar[str | None] = None

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

    def _manifest_extract_inputs(self) -> dict[str, Any]:
        """Load the connector's manifest and return its extract node's ``args``.

        Raises if the manifest is missing or has no ``dag.extract.inputs.args``
        — a manifest-driven SDR test with no usable extract node is a config
        error, not something to silently fall back on.

        Note: the sibling ``inputs.workflow_type`` (the AE workflow-type slug) is
        deliberately NOT read — see :meth:`_workflow_args_from_manifest`.
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
            # conformance: ignore[E012] this is a test-authoring helper (BaseSDRIntegrationTest), not activity/task code with AppError plumbing; an existing test pins ValueError
            raise ValueError(
                f"Manifest at {path} has no `dag.extract.inputs.args` object — "
                "cannot derive the workflow input from it."
            )
        return inputs["args"]

    def _workflow_args_from_manifest(self, base_args: dict[str, Any]) -> dict[str, Any]:
        """Build workflow input from the manifest's extract args + substitutions.

        Only ``dag.extract.inputs.args`` is substituted — exactly like the e2e /
        full_dag walker. The base fills the *universal* SDR slots every
        SDR-capable connector has (``connection``, ``credential`` /
        ``credential-guid``, ``extraction-method``, ``agent-json``,
        ``preflight-check``). Every other ``{{placeholder}}`` the manifest
        declares is connector-specific — SQL's ``{{include-filter}}`` /
        ``{{exclude-filter}}`` / table regexes, or whatever another app's contract
        defines — and is resolved from the per-scenario ``metadata`` (keyed by the
        placeholder name), so the base stays connector-agnostic. Any placeholder
        no scenario value covers is defaulted to "" (so it never leaks a literal
        ``{{...}}``) AND logged at WARNING — for a filter slot that is
        extract-everything, but for a non-filter slot (e.g. ``{{target}}``,
        ``{{lookback-days}}``) "" is an empty/invalid value, i.e. a likely-missing
        required input, so the warning flags it rather than silently masking it.

        ``{{credential-guid}}`` is filled with "" rather than the AE-runtime
        ``"{{credentialGuid}}"``; agent mode resolves the credential via
        ``agent_json``. Note this is "absent" only at the *workflow* layer — the
        client still sends inline credentials (an empty-string guid is present in
        the body, so ``_call_workflow`` does not treat it as missing). For SDR
        (agent mode) the end state matches; a direct-mode suite opted into this
        would diverge from the legacy local-vault provisioning path.
        """
        extract_args = self._manifest_extract_inputs()
        method = "agent" if self.agent_spec_template else "direct"
        # A manifest-driven suite with no agent_spec_template silently degrades to
        # direct mode (no agent spec injected) — warn so the misconfig isn't silent.
        if not self.agent_spec_template:
            logger.warning(
                "SDR manifest %s: manifest_path is set but agent_spec_template is "
                "empty — extraction-method falls back to 'direct' and no agent spec "
                "is injected. Set agent_spec_template for an SDR/agent-mode run.",
                self.manifest_path,
            )
        metadata = base_args.get("metadata") or {}
        # Universal SDR slots — present in every SDR-capable connector's extract args.
        subs: dict[str, Any] = {
            "{{credential}}": None,
            "{{credential-guid}}": "",
            "{{connection}}": base_args.get("connection") or {},
            "{{extraction-method}}": method,
            "{{agent-json}}": self.agent_spec_template or None,
            "{{preflight-check}}": True,
        }
        # Connector-specific placeholders are NOT enumerated here — each scenario
        # supplies them via metadata (keyed by placeholder name). A metadata key
        # never overrides a universal slot above — and a collision with one is
        # dropped (setdefault is a no-op against the pre-seeded value), so warn
        # rather than silently ignore it (mirrors the hyphen-vs-underscore warning).
        for key, value in metadata.items():
            placeholder = "{{" + key + "}}"
            if placeholder in subs:
                logger.warning(
                    "SDR manifest %s: scenario metadata key %r collides with a "
                    "reserved universal SDR slot — ignored (the universal value is "
                    "used). Rename it if it was meant to be connector-specific.",
                    self.manifest_path,
                    key,
                )
                continue
            subs[placeholder] = value

        placeholders = _collect_placeholders(extract_args)
        # A metadata key that matches no placeholder (e.g. hyphen-vs-underscore:
        # metadata ``include-filter`` against a manifest ``{{include_filter}}``)
        # would otherwise be silently ignored — surface it.
        for key in metadata:
            if "{{" + key + "}}" not in placeholders:
                logger.warning(
                    "SDR manifest %s: scenario metadata key %r matches no "
                    "{{%s}} placeholder in the extract args — value ignored "
                    "(check hyphen vs underscore spelling).",
                    self.manifest_path,
                    key,
                    key,
                )
        # Default any still-unresolved placeholder to "" so it can't leak a literal
        # {{...}} into the workflow input — but WARN, because this is only
        # "extract-everything" for a filter slot; for a non-filter slot (e.g.
        # {{target}}, {{lookback-days}}) "" is an empty/invalid value and most
        # likely a missing required input that should be supplied via metadata.
        for placeholder in placeholders:
            if placeholder not in subs:
                logger.warning(
                    "SDR manifest %s: placeholder %s had no value (not a universal "
                    "SDR slot, not a scenario-metadata key) — defaulting to ''. If "
                    "it is a required input, add it to the scenario's metadata.",
                    self.manifest_path,
                    placeholder,
                )
                subs[placeholder] = ""
        wf_args = _apply_mustache_subs(extract_args, subs)
        # Carry credentials through for the start endpoint (agent mode resolves
        # via agent_json; direct mode + the client's credential provisioning use
        # these). The manifest-substituted args are authoritative for the run;
        # metadata is forwarded only for backward-compat with the start handler.
        merged: dict[str, Any] = {
            k: base_args[k] for k in ("credentials", "metadata") if k in base_args
        }
        merged.update(wf_args)
        # Entrypoint stays class-controlled: self.workflow_type maps to an SDK
        # entrypoint name (default None → the app's implicit "run"). The
        # manifest's sibling inputs.workflow_type is the AE workflow-type slug —
        # a DIFFERENT namespace — and must NOT be sent as the start-body
        # workflow_type, or the SDK rejects it as an invalid entrypoint (400).
        if self.workflow_type:
            merged["workflow_type"] = self.workflow_type
        return merged

    @staticmethod
    def _sdr_flag(env_name: str, default: bool) -> bool:
        """Effective boolean for a guard: an env override wins over the class
        default. The ``sdr-e2e`` action sets these (e.g. from a fleet allowlist)
        to enable a guard for a connector WITHOUT editing the connector's suite —
        so the whole SDR harness stays SDK-side. Env unset → the class default.
        """
        val = os.environ.get(env_name)
        if val is not None:
            return val.strip().lower() in ("1", "true", "yes", "on")
        return default

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
                if self._sdr_flag(
                    "SDR_REQUIRE_ASSETS_LANDED", self.require_assets_landed
                ):
                    self._assert_assets_landed(scenario, result.response)
                if self._sdr_flag(
                    "SDR_REQUIRE_UPSTREAM_ASSETS_LANDED",
                    self.require_upstream_assets_landed,
                ):
                    self._assert_upstream_assets_landed(scenario, result.response)
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
        elif (
            scenario.api.lower() == "workflow"
            and result.success
            and (
                scenario.expected_data
                or scenario.schema_base_path
                or self.schema_base_path
            )
            and (
                self._sdr_flag("SDR_REQUIRE_ASSETS_LANDED", self.require_assets_landed)
                or self._sdr_flag(
                    "SDR_REQUIRE_UPSTREAM_ASSETS_LANDED",
                    self.require_upstream_assets_landed,
                )
            )
        ):
            # An assets-landed guard is requested, but this scenario validates
            # via expected_data/schema_base_path — so the guard block above is
            # skipped and the check silently does NOT run. Surface it so a green
            # tick isn't mistaken for guard coverage.
            logger.warning(
                "SDR workflow scenario %r: an assets-landed guard is enabled "
                "(SDR_REQUIRE_ASSETS_LANDED / SDR_REQUIRE_UPSTREAM_ASSETS_LANDED) but "
                "the scenario validates via expected_data/schema_base_path, so the "
                "guard is NOT applied here. Use a scenario without "
                "expected_data/schema_base_path to enforce the assets-landed check.",
                scenario.name,
            )
        return result

    # ------------------------------------------------------------------
    # SDR readiness — dynamic assertions (assets actually landed, correctly)
    # ------------------------------------------------------------------

    def _sdr_connection_qn(self) -> str | None:
        """The connection qualifiedName every extracted asset should be nested
        under, from ``default_connection`` — the nested AE shape
        (``{"attributes": {"qualifiedName": ...}}``) or a flat
        ``{"connection_qualified_name": ...}``. ``None`` when unresolved.
        """
        conn = self.default_connection or {}
        if not isinstance(conn, dict):
            return None
        attrs = conn.get("attributes")
        if isinstance(attrs, dict) and attrs.get("qualifiedName"):
            return str(attrs["qualifiedName"])
        if conn.get("connection_qualified_name"):
            return str(conn["connection_qualified_name"])
        return None

    def _assert_assets_landed(
        self, scenario: Scenario, response: dict[str, Any]
    ) -> None:
        """After a workflow scenario reaches COMPLETED, assert the extraction
        actually WROTE assets — at the expected output path, in non-zero count,
        and (opt-in) nested under the connection's qualifiedName prefix.

        A run that completes with status 'success' but writes zero assets — or
        writes them to the wrong prefix — is the classic silent SDR failure the
        dynamic run must catch (a static check can't see it). Only runs when an
        ``extracted_output_base_path`` is known; otherwise warns.
        """
        base_path = (
            os.environ.get("SDR_EXTRACTED_OUTPUT_BASE_PATH")
            or scenario.extracted_output_base_path
            or self.extracted_output_base_path
        )
        if not base_path:
            logger.warning(
                "SDR workflow scenario %r completed, but no extracted_output_base_path "
                "is set — cannot verify assets landed. Set it (scenario or class) to "
                "catch 'COMPLETED but zero assets' / wrong-path silent failures.",
                scenario.name,
            )
            return

        data = response.get("data", {})
        workflow_id, run_id = data.get("workflow_id"), data.get("run_id")
        # A COMPLETED run that doesn't return both IDs can't be located — surface
        # the clean diagnostic here instead of a TypeError from os.path.join(...,
        # None) downstream in load_actual_output.
        if not workflow_id or not run_id:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}' completed with status "
                f"'success' but the response is missing workflow_id/run_id "
                f"(workflow_id={workflow_id!r}, run_id={run_id!r}) — cannot locate "
                f"the extracted output to verify assets landed."
            )
        from application_sdk.testing.integration.comparison import (  # noqa: PLC0415
            load_actual_output,
        )

        # load_actual_output reads {base}/{workflow_id}/{run_id}/{subdirectory}/ and
        # raises FileNotFoundError when that dir is missing/empty — so this single
        # call asserts BOTH "assets landed" (count > 0) AND "at the right path" (a
        # dropped {workflow_id}/{run_id} segment surfaces here as FileNotFoundError).
        try:
            records = load_actual_output(
                base_path,
                workflow_id,
                run_id,
                subdirectory=scenario.output_subdirectory,
            )
        # conformance: ignore[E004] re-raised as AssertionError carrying the diagnostic
        except FileNotFoundError as exc:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}' completed with status "
                f"'success' but NO extracted assets landed at "
                f"{base_path}/{workflow_id}/{run_id} — the classic SDR silent-success / "
                f"zero-asset (or wrong-egress-path) failure. ({exc})"
            ) from exc
        if not records:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}' completed but produced ZERO "
                f"asset records under {base_path}/{workflow_id}/{run_id}."
            )

        # Correct location: every asset must be nested under the connection QN.
        if self.require_asset_connection_prefix:
            conn_qn = self._sdr_connection_qn()
            if not conn_qn:
                logger.warning(
                    "SDR workflow scenario %r: %d assets landed, but the connection "
                    "qualifiedName is unresolved (set default_connection) — skipping "
                    "the asset-location (connection-prefix) check.",
                    scenario.name,
                    len(records),
                )
            else:
                # Segment-boundary match: the connection asset itself (qn ==
                # conn_qn) or a descendant (conn_qn + "/..."). A bare startswith
                # would treat "default/oracle/17300" as nested under
                # "default/oracle/1730".
                misplaced = [
                    qn
                    for r in records
                    for qn in [(r.get("attributes") or {}).get("qualifiedName")]
                    if isinstance(qn, str)
                    and qn
                    and qn != conn_qn
                    and not qn.startswith(conn_qn + "/")
                ]
                assert not misplaced, (
                    f"SDR workflow scenario '{scenario.name}': {len(misplaced)} of "
                    f"{len(records)} extracted assets are NOT nested under the "
                    f"connection qualifiedName '{conn_qn}' — assets landed at the wrong "
                    f"location (mis-parented / dropped connection prefix). Examples: "
                    f"{misplaced[:3]}"
                )

        # Per-type count floors (opt-in): partial / mis-typed extraction can land
        # SOME assets — passing the non-zero + location guards above — yet miss a
        # whole type (e.g. Tables land but Columns never do). Assert each expected
        # typeName meets its floor. Records carry a top-level ``typeName`` (same
        # shape the integration comparison groups on).
        if self.expected_min_asset_counts:
            counts = Counter(r.get("typeName") for r in records if r.get("typeName"))
            shortfalls = [
                f"{type_name}: expected >= {floor}, got {counts.get(type_name, 0)}"
                for type_name, floor in self.expected_min_asset_counts.items()
                if counts.get(type_name, 0) < floor
            ]
            assert not shortfalls, (
                f"SDR workflow scenario '{scenario.name}': extracted assets do not meet "
                f"the expected per-type minimum counts (partial / mis-typed extraction "
                f"that the non-zero guard cannot catch). Shortfalls: {shortfalls}. "
                f"Observed counts: {dict(counts)}"
            )
        logger.info(
            "SDR workflow scenario %r: %d asset record(s) landed at %s/%s/%s",
            scenario.name,
            len(records),
            base_path,
            workflow_id,
            run_id,
        )

    def _assert_upstream_assets_landed(
        self, scenario: Scenario, response: dict[str, Any]
    ) -> None:
        """After a workflow scenario reaches COMPLETED, assert the transformed
        assets also reached the UPSTREAM (atlan) object store — the store the
        atlan-side Publish app actually reads.

        In SDR (two-store) mode the connector copies ``transformed/`` from the
        deployment store to the upstream store (``upload_to_atlan`` /
        ``ENABLE_ATLAN_UPLOAD``). A green extraction whose deployment store has
        assets but whose upstream ``transformed/`` is empty makes Publish emit
        ZERO assets (atlan-looker-app#134) — a failure the deployment-store check
        cannot see. Only runs when ``upstream_output_base_path`` is set (the
        two-store CI stack); otherwise warns.
        """
        base_path = (
            os.environ.get("SDR_UPSTREAM_OUTPUT_BASE_PATH")
            or self.upstream_output_base_path
        )
        if not base_path:
            logger.warning(
                "SDR workflow scenario %r: require_upstream_assets_landed is set but "
                "upstream_output_base_path is not — cannot verify the upstream (atlan) "
                "object store. Point it at the two-store CI stack's upstream mount.",
                scenario.name,
            )
            return

        data = response.get("data", {})
        workflow_id, run_id = data.get("workflow_id"), data.get("run_id")
        if not workflow_id or not run_id:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}' completed but the response is "
                f"missing workflow_id/run_id — cannot locate the upstream output."
            )
        from application_sdk.testing.integration.comparison import (  # noqa: PLC0415
            load_actual_output,
        )

        try:
            records = load_actual_output(
                base_path,
                workflow_id,
                run_id,
                subdirectory=scenario.output_subdirectory,
            )
        # conformance: ignore[E004] re-raised as AssertionError carrying the diagnostic
        except FileNotFoundError as exc:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}' completed and landed assets in "
                f"the deployment store, but the UPSTREAM (atlan) object store's "
                f"transformed/ at {base_path}/{workflow_id}/{run_id} is EMPTY — the "
                f"atlan-side Publish app would publish ZERO assets. The connector's "
                f"transformed output never reached the upstream store (missing/broken "
                f"upload_to_atlan, or ENABLE_ATLAN_UPLOAD not honoured). ({exc})"
            ) from exc
        if not records:
            raise AssertionError(
                f"SDR workflow scenario '{scenario.name}': the upstream (atlan) object "
                f"store's transformed/ under {base_path}/{workflow_id}/{run_id} has ZERO "
                f"records — Publish would publish nothing."
            )
        logger.info(
            "SDR workflow scenario %r: %d transformed record(s) reached the upstream "
            "(atlan) object store at %s/%s/%s",
            scenario.name,
            len(records),
            base_path,
            workflow_id,
            run_id,
        )

    def test_sdr_suite_runs_an_extraction(self) -> None:
        """Readiness floor: an agent-mode SDR suite should validate a real
        extraction, not just auth/preflight. A suite with no ``api="workflow"``
        scenario never exercises the extract → transform → asset-landing path SDR
        actually runs for a customer.

        Advisory by default (skips with a message) so it can be adopted without a
        fleet-wide red; set ``enforce_workflow_floor = True`` on the suite to make
        it a hard failure once an extraction scenario exists.
        """
        if not self.agent_spec_template:
            pytest.skip("not an agent-mode SDR suite (no agent_spec_template)")
        if any(s.api.lower() == "workflow" for s in self.scenarios):
            return
        msg = (
            "SDR suite declares no api='workflow' scenario — it validates auth / "
            "preflight only and never runs a real extraction. Add a "
            "workflow-to-completion scenario so SDR readiness reflects a real run."
        )
        if self.enforce_workflow_floor:
            raise AssertionError(msg)
        pytest.skip(msg + " (advisory; set enforce_workflow_floor=True to require it)")

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

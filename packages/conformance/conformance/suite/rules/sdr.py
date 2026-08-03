"""SDR-readiness rule definitions (P029, P030, P037, P038, P039, P041).

Apps that declare ``self_deployed_runtime: true`` in ``atlan.yaml`` must satisfy
six structural invariants before they can be considered SDR-ready:

* ``P029`` — every ``manifest.json`` under ``app/generated/`` must include an
  ``agent_json`` key inside ``dag.extract.inputs.args``.  Missing this field
  causes a silent production failure: the SDR worker starts, the workflow
  completes with status "success", but no credentials are routed to the
  extraction agent — assets never move to the Atlan bucket.  The MSSQL
  connector regression (atlan-mssql-app#177) is the canonical example.

* ``P030`` — at least one Python source file (outside ``tests/``) must contain
  a ``self.upload(`` call.  Without it the ``ENABLE_ATLAN_UPLOAD`` path is
  never reached: extraction "passes" (workflow status = success) but no assets
  are transferred to the Atlan tenant bucket.  The regression slipped because
  the SDR test pipeline validated only workflow *status*, not output.  Does
  not apply to apps with no publish stage (``pipeline.publish = null`` in
  ``contract/app.pkl``, reflected as no ``dag.publish`` node in the generated
  manifest) — there is nowhere for such an app to hand extracted assets off to.

* ``P037`` — an app that resolves source credentials with a custom, GUID-only
  path (a hand-rolled vault read + ``resolve_credential_raw`` or a bare
  ``CredentialRef(credential_guid=...)`` construction) but never routes through
  an agent-aware resolver (``CredentialRef.resolve`` /
  ``CredentialRef.from_workflow_args``).  The manifest can be P029-clean and
  ``agent_json`` forwarded, but code that resolves strictly by
  ``credential_guid`` ignores it, so agent-mode credentials never resolve and
  the app writes zero assets (observed for a table-format connector in fleet
  testing).

* ``P038`` — an app that roots its object-store output prefix
  (``artifacts/apps/<identity>/...``) from a workflow-input ``application_name``
  field (contract default ``""``) instead of the SDK app identity.  AE forwards
  only manifest-declared args, so the field stays empty and artifacts land under
  a mis-rooted path (empty app segment); ``self.upload()`` succeeds but 0 assets
  publish (observed for a document-store connector in fleet testing).

* ``P039`` — an app whose generated manifest declares ``{{agent-json}}`` (so P029
  passes) but whose generated extract-input contract (``AppInputContract`` in a
  generated ``_input.py``) subclasses the bare ``Input`` base, declares no
  ``agent_json`` field, and rejects extra fields — so Pydantic silently drops the
  forwarded ``agent_json`` and credentials never resolve (``PipelineContractError``
  / 0 assets; observed for a BI connector in fleet testing).  Contracts that
  subclass the SDK ``*ExtractionInput`` family or allow extra fields are exempt.

* ``P041`` — an SDR app that hard-codes ``preflight_gate_mode = "hard"``.  In
  SDR (agent) mode, preflight checks that depend on *non-secret* config (e.g. a
  database/schema existence check) can fail because customers rightly do not
  mirror non-sensitive values into the secret store; a hard gate then aborts
  the whole workflow (observed for a query-engine connector in fleet testing).
  Suggest-only: prefer a run-mode-differentiated posture.

All rules are APP-scoped (the SDK itself does not declare ``self_deployed_runtime``
and is therefore always skipped) and gate on ``self_deployed_runtime: true``
being present in ``atlan.yaml``.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P029",
        scope=RuleScope.APP,
        name="SdrManifestMissingAgentJson",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "In SDR (agent) mode the platform (Heracles/AE) derives the customer "
            "agent task queue and fills the credential-routing spec from "
            "agent_json + extraction_method at the TOP LEVEL of "
            "dag.extract.inputs.args. If an agent manifest nests them only under "
            "args.metadata, the platform can't see them and strands the agent "
            "extraction on the cloud queue (atlan-tableau-app / atlan-snowflake-app); "
            "if it omits them entirely the agent receives no credentials and "
            "produces zero assets — the MSSQL silent-failure regression "
            "(atlan-mssql-app#177, DISTR-752). Either way the workflow reports "
            "'success', invisible to status-only test pipelines."
        ),
        short_description=(
            "SDR agent manifest must surface agent_json + extraction_method at the "
            "top level of dag.extract.inputs.args"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "every *agent extraction* ``manifest.json`` under ``app/generated/``\n"
            "must surface both ``agent_json`` and ``extraction_method`` at the TOP\n"
            "LEVEL of ``dag.extract.inputs.args``. An agent extraction is any node\n"
            "whose args carry the ``{{agent-json}}`` routing placeholder; a\n"
            "miner/QI or ``clean`` entrypoint that carries none is exempt.\n"
            "\n"
            "In SDR (agent) mode the platform (Heracles/AE) derives the agent task\n"
            "queue (``atlan-<agent-name>``) and fills the credential-routing spec\n"
            "from these top-level fields at dispatch. Two failure modes:\n"
            "\n"
            "* **Misplaced / partial** — the fields are nested only under\n"
            "  ``args.metadata`` (or ``extraction_method`` is omitted). The platform\n"
            "  can't derive the queue, so the extraction strands on the cloud queue\n"
            "  ``atlan-<app>-<deployment>`` — which the customer's firewalled source\n"
            "  can't reach — and never progresses (atlan-tableau-app,\n"
            "  atlan-snowflake-app).\n"
            "* **Missing entirely** — no manifest declares agent routing at all; the\n"
            "  agent receives no credentials and writes zero assets while the\n"
            "  workflow reports success (atlan-mssql-app#177). The hand-written SDR\n"
            "  test supplied the value directly, bypassing the manifest, so it\n"
            "  passed; adopting ``BaseSDRIntegrationTest.manifest_path`` (T003)\n"
            "  closes the test gap, this rule closes the static gap.\n"
            "\n"
            "**Remediation:** surface ``agent_json`` + ``extraction_method`` at the\n"
            "extract-args top level in the app's ``contract/app.pkl`` (keep them\n"
            "under ``metadata`` too if the connector reads there) and re-run\n"
            "``pkl eval`` to regenerate ``app/generated/<name>/manifest.json``.  Do\n"
            "not hand-edit the generated manifest — C002 tracks drift.\n"
            "\n"
            "Toolkit-version notes (from fleet remediation):\n"
            "\n"
            "* Manifests generated by **older contract-toolkit versions** may not\n"
            "  emit the ``{{agent-json}}`` slot at all — the missing-entirely mode\n"
            "  above.  The fix is to **regenerate with the current toolkit**, not to\n"
            "  hand-patch the slot in.\n"
            "* ``flatManifestArgs: true`` is required in the contract so\n"
            "  ``agent_json`` + ``extraction_method`` land at the extract-args TOP\n"
            "  LEVEL; with ``flatManifestArgs: false`` the toolkit buries them under\n"
            "  ``args.metadata``, which is exactly the misplaced mode above\n"
            "  (agent-queue routing breaks when the fields are only in metadata).\n"
            "* The platform (AE) fetches the manifest from the **deployed app\n"
            "  service**, not from the repo — a manifest fix only takes effect once\n"
            "  the deployed image ships it.  A merged PR alone does not clear the\n"
            "  runtime failure.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p029"
        ),
    ),
    RuleDefinition(
        id="P030",
        scope=RuleScope.APP,
        name="SdrUploadNotCalled",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "In SDR mode the ENABLE_ATLAN_UPLOAD env var gates whether extracted "
            "assets are transferred to the Atlan tenant bucket. If the app never "
            "calls self.upload(), the ENABLE_ATLAN_UPLOAD path is unreachable: "
            "extraction completes with status 'success' but no assets land in the "
            "bucket — a regression that slipped through status-only SDR CI (DISTR-752). "
            "This rule detects the structural absence of the upload call in app source "
            "so the regression class is caught at static-analysis time rather than "
            "in a customer deployment. Fleet remediation confirmed the finding is "
            "REAL more often than assumed: 4 of 15 swept connectors had a genuine "
            "silent-zero-asset publish behind a P030 finding that had been "
            "presumed a false positive."
        ),
        short_description=(
            "SDR app has no self.upload() call in source — ENABLE_ATLAN_UPLOAD path unreachable"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "at least one Python source file (outside ``tests/``) must contain a\n"
            "``self.upload(`` call.\n"
            "\n"
            "``App.upload()`` is the SDK's sanctioned way to transfer extracted\n"
            "assets to the Atlan tenant bucket (the upstream store) in SDR mode.\n"
            "It is gated by ``ENABLE_ATLAN_UPLOAD``: when the env var is ``true``\n"
            "the upload runs and assets land in the bucket; when false, the app\n"
            "runs in a local-only mode.  If ``self.upload()`` is never called\n"
            "anywhere in the app source, the gate is structurally unreachable —\n"
            "the workflow completes with status 'success' regardless of the flag\n"
            "value, and no assets move to the bucket in production.\n"
            "\n"
            "**What does NOT satisfy this rule** (patterns fleet remediation found\n"
            "behind real silent-zero-asset publishes):\n"
            "\n"
            "* **Delegating to SDK ``SqlApp.run()``** — ``run()`` persists\n"
            "  extraction output to the *deployment* store only; the publish stage\n"
            "  reads the *tenant bucket*.  Extraction 'succeeds' and 0 assets\n"
            "  publish (observed for an OLAP connector in fleet testing).\n"
            "* **A no-op ``upload_to_atlan`` stub** — a method defined with no\n"
            "  storage-transfer call in its body (e.g. a body that only logs or\n"
            "  returns, with a comment claiming another stage owns the transfer).\n"
            "  The checker flags these definitions specifically (observed for a\n"
            "  document-store connector in fleet testing).  A transfer is matched\n"
            "  on whole ``_``-separated tokens of the callee name, never a bare\n"
            "  substring, so ``compute_summary`` / ``output_stats`` /\n"
            "  ``get_inputs`` do not read as transfers while ``upload_file`` /\n"
            "  ``storage_upload_file`` / ``migrate_from_objectstore_to_atlan`` do;\n"
            "  a bare verb (``sync``, ``push``, ``put``, ``copy``) additionally\n"
            "  requires a store-naming receiver.  Delegation is resolved one level\n"
            "  into the same class, and an abstract declaration\n"
            "  (``raise NotImplementedError`` / ``pass`` / ``...``) is not a stub.\n"
            "* **A deprecated SDK upload shim** that re-roots artifacts under the\n"
            "  code-derived app name and drops the ``transformed/`` segment — the\n"
            "  transfer runs but publish finds nothing at the expected prefix\n"
            "  (observed for a managed-Postgres connector in fleet testing; see\n"
            "  also P038 for the mis-rooting class).\n"
            "* **Inline writers that target the deployment store only** — output is\n"
            "  written, but never to the tenant bucket (observed for a key-value\n"
            "  store connector in fleet testing).\n"
            "\n"
            "**Never mark a P030 finding a false positive without a green full-DAG\n"
            "e2e** (extract → publish) proving assets actually land in Atlas.  The\n"
            "workflow status is not evidence — every failure mode above reports\n"
            "'success'.  The live e2e's asset-count floor is the arbiter.\n"
            "\n"
            "This is a WARN (not BLOCK): some apps are legitimately preflight-only\n"
            "or delegate upload to a base-class method defined in the SDK template,\n"
            "and apps with a *working* custom key-preserving deployment-to-upstream\n"
            "bridge (an ``upload_to_atlan`` that performs real storage transfers\n"
            "and preserves the ``workflows/{workflow_id}/{run_id}/`` key layout)\n"
            "are documented false positives once a green full-DAG e2e proves assets\n"
            "land.  Review the finding before suppressing — if the app genuinely\n"
            "performs an extract-and-upload cycle with no working bridge, add\n"
            "``await self.upload(...)`` to the ``run()`` method or the relevant\n"
            "``@entrypoint`` method, or wire a key-preserving ``upload_to_atlan``\n"
            "bridge into every entrypoint (crawler AND miner).\n"
            "\n"
            "Note: P008 flags ``self.upload()`` *inside* ``@task`` methods (the\n"
            "wrong location); P030 flags the *absence* of any upload call.  They\n"
            "are complementary: both should be clean for a correctly-wired SDR app.\n"
            "\n"
            "Exemption: this rule is skipped for apps whose ``contract/app.pkl``\n"
            "sets ``pipeline.publish = null`` (no publish stage), which compiles\n"
            "to a generated manifest with no ``dag.publish`` node.  An extract-only\n"
            "app has nothing to hand the extracted assets off to, so the absence\n"
            "of ``self.upload()`` is by design, not a gap.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p030"
        ),
    ),
    RuleDefinition(
        id="P037",
        scope=RuleScope.APP,
        name="SdrAgentJsonNotConsumed",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.16.0",
        rationale=(
            "In SDR (agent) mode the platform forwards agent_json on the workflow "
            "input; the SDK's agent-aware resolvers (CredentialRef.resolve / "
            "CredentialRef.from_workflow_args) consume it to route the credential "
            "fetch through the customer-side agent. An app that instead resolves "
            "credentials with a custom, GUID-only path — a hand-rolled vault read "
            "plus resolve_credential_raw, or a bare CredentialRef(credential_guid=...) "
            "— ignores agent_json entirely. Its manifest can be P029-clean, but in "
            "agent mode the credential never resolves and the workflow writes zero "
            "assets while reporting 'success' (observed for a table-format connector "
            "in fleet testing)."
        ),
        short_description=(
            "SDR app resolves credentials by credential_guid only and never routes "
            "through an agent-aware resolver — agent_json is ignored"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "credential resolution must be able to consume ``agent_json`` — the\n"
            "agent-mode routing spec the platform forwards on the workflow input.\n"
            "\n"
            "This rule fires when an app performs *custom* credential resolution —\n"
            "a bare ``CredentialRef(credential_guid=...)`` construction or a\n"
            "``resolve_credential_raw(...)`` call — but NEVER routes through an\n"
            "agent-aware resolver entry point anywhere in its source:\n"
            "\n"
            "* ``CredentialRef.resolve(input)`` — reads ``input.agent_json`` and\n"
            "  picks the agent vs. direct-GUID route.\n"
            "* ``CredentialRef.from_workflow_args(workflow_args)`` — the same,\n"
            "  reading ``agent_json`` off the args payload.\n"
            "\n"
            "An app that resolves strictly by ``credential_guid`` (a custom local\n"
            "vault read that only ever builds ``CredentialRef(name=guid,\n"
            "credential_guid=guid)``) ignores ``agent_json``, so in agent mode the\n"
            "credential never resolves and zero assets are written — a silent\n"
            "failure invisible to status-only pipelines.\n"
            "\n"
            "Apps that lean on the SDK's transparent resolution (they build no\n"
            "``CredentialRef`` and call no ``resolve_credential_raw``) are not gated\n"
            "in and never flagged.\n"
            "\n"
            "This is a WARN (not BLOCK): the static heuristic recognises the two\n"
            "sanctioned resolver entry points and a direct ``agent_spec``-carrying\n"
            "ref, but an app could resolve ``agent_json`` through a bespoke helper\n"
            "the heuristic does not know about.  Review before suppressing.\n"
            "\n"
            "**Remediation:** route credential resolution through\n"
            "``CredentialRef.resolve(input)`` or\n"
            "``CredentialRef.from_workflow_args(workflow_args)`` (both consume\n"
            "``agent_json`` and pick the correct route), keeping the direct\n"
            "``credential_guid`` path only as a fallback after the agent-aware call.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p037"
        ),
    ),
    RuleDefinition(
        id="P038",
        scope=RuleScope.APP,
        name="SdrArtifactMisrooted",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.16.0",
        rationale=(
            "The object-store output prefix must be rooted from the SDK app identity "
            "(APPLICATION_NAME / self._app_name), which the SDK's own "
            "WORKFLOW_OUTPUT_PATH_TEMPLATE does correctly. An app that instead roots "
            "'artifacts/apps/<identity>/...' from the workflow-input application_name "
            "field mis-roots: that field's contract default is '' and AE forwards "
            "only manifest-declared args, so it stays empty and artifacts land under "
            "'artifacts/apps//workflows/...' (empty app segment). self.upload() then "
            "succeeds but 0 assets publish — P030 passes the app (upload IS called), "
            "so this distinct rule catches the wrong-root case (observed for a "
            "document-store connector in fleet testing)."
        ),
        short_description=(
            "SDR object-store prefix rooted from the empty-defaulting input "
            "application_name field instead of APPLICATION_NAME"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "the object-store output path/prefix\n"
            "(``artifacts/apps/<identity>/workflows/...``) must be rooted from the\n"
            "SDK app identity — the ``APPLICATION_NAME`` constant, ``self._app_name``,\n"
            "or the SDK's ``WORKFLOW_OUTPUT_PATH_TEMPLATE`` (which fills\n"
            "``application_name`` from the app identity).\n"
            "\n"
            "This rule fires when the app instead builds that path from a\n"
            "*workflow-input* ``application_name`` field — read as\n"
            '``input_data.get("application_name", ...)``,\n'
            '``input_data["application_name"]``, or ``input.application_name`` — and\n'
            "interpolates it (directly or via a local variable) into an f-string\n"
            "whose literal contains ``artifacts/apps``.  The contract default of\n"
            'that field is ``""`` and AE forwards only manifest-declared args, so\n'
            "the value stays empty; the artifacts then land under\n"
            "``artifacts/apps//workflows/...`` (note the empty app segment).\n"
            "``self.upload()`` reports success but the publish app finds 0 assets at\n"
            "the expected prefix.\n"
            "\n"
            "This is complementary to P030: P030 checks that ``self.upload()`` is\n"
            "*called*; P038 checks that what it uploads is rooted correctly.  An app\n"
            "can pass P030 and still fail P038.\n"
            "\n"
            "This is a WARN (not BLOCK): the failure is a silent zero-asset publish,\n"
            "not a hard crash, and the heuristic is deliberately narrow (it keys on\n"
            "the ``application_name`` input field feeding an ``artifacts/apps``\n"
            "literal).  It does NOT catch every mis-rooting — an app that forwards an\n"
            "empty ``output_prefix`` input field without an ``artifacts/apps`` literal\n"
            "is indistinguishable from a correct app statically and is left to\n"
            "runtime/e2e detection.\n"
            "\n"
            "**Remediation:** root the object-store prefix from ``APPLICATION_NAME`` /\n"
            "``self._app_name`` (or use ``WORKFLOW_OUTPUT_PATH_TEMPLATE.format(\n"
            "application_name=APPLICATION_NAME, ...)``), or default the contract field\n"
            "to the app name; never derive it from an empty-defaulting workflow arg.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p038"
        ),
    ),
    RuleDefinition(
        id="P039",
        scope=RuleScope.APP,
        name="SdrAgentJsonDroppedByInputContract",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.16.0",
        rationale=(
            "In SDR (agent) mode the platform forwards agent_json on the extract "
            "input. A generated extract-input contract that subclasses the bare Input "
            "base, declares no agent_json field, and rejects extra fields makes "
            "Pydantic silently drop that forwarded agent_json — the extract input's "
            "credential_ref is then None and extraction fails with PipelineContractError "
            "and 0 assets, even though the manifest is P029-clean (declares "
            "{{agent-json}}). This is distinct from P029 (manifest side) and P037 "
            "(code resolves by guid only): here the manifest and code are fine but the "
            "typed contract eats the field (observed for a BI connector in fleet "
            "testing)."
        ),
        short_description=(
            "SDR generated extract-input contract drops the forwarded agent_json "
            "(bare Input subclass, no agent_json field, extra fields rejected)"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``\n"
            "whose generated manifest declares agent routing (the ``{{agent-json}}``\n"
            "placeholder at the extract-args top level — i.e. P029 passes), the\n"
            "generated extract-input contract model must be able to *receive* the\n"
            "forwarded ``agent_json``.\n"
            "\n"
            "This rule fires when the generated extract-input contract\n"
            "(``AppInputContract`` in a generated ``_input.py``):\n"
            "\n"
            "* subclasses the bare ``Input`` base (NOT the SDK ``*ExtractionInput``\n"
            "  family, which declares ``agent_json``), AND\n"
            "* declares no ``agent_json`` field of its own, AND\n"
            "* rejects extra fields (no ``allow_unbounded_fields=True`` class keyword,\n"
            '  no ``extra="allow"`` in the model config).\n'
            "\n"
            "In that shape Pydantic silently drops the forwarded ``agent_json`` at\n"
            "model construction. The extract input's ``credential_ref`` is then\n"
            '``None`` and extraction fails with ``PipelineContractError`` ("No\n'
            'credential_ref or inline_credentials on input") — 0 assets — while the\n'
            "manifest and the connector code both look correct.\n"
            "\n"
            "This is orthogonal to P029 (which checks the *manifest*) and P037 (which\n"
            "checks that the *code* consumes ``agent_json``): all three must be clean.\n"
            "\n"
            "This is a WARN (not BLOCK): the heuristic reads the generated contract's\n"
            "declared bases, fields, and model config; an app that receives\n"
            "``agent_json`` through a base the heuristic does not recognise could be\n"
            "flagged.  Review before suppressing.\n"
            "\n"
            "**Remediation:** declare ``agent_json`` on the extract-input contract in\n"
            "``contract/app.pkl`` and regenerate, subclass the SDK ``ExtractionInput``\n"
            "family (which declares it), or set ``allow_unbounded_fields=True`` on the\n"
            "contract — as the passing counterexample connectors in fleet testing do.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p039"
        ),
    ),
    RuleDefinition(
        id="P041",
        scope=RuleScope.APP,
        name="SdrHardPreflightGate",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "In SDR (agent) mode the app resolves its configuration through the "
            "customer's secret store, and customers rightly mirror only *secrets* "
            "into it — non-sensitive values (database names, schema filters) are "
            "often absent. Preflight checks that depend on such non-secret config "
            "then report NOT_READY even though the run would succeed. Most "
            "connectors run the preflight gate non-fatally, but an app that "
            "hard-codes preflight_gate_mode = 'hard' aborts the entire workflow on "
            "that spurious verdict (observed for a query-engine connector in fleet "
            "testing). Until the SDK differentiates gate enforcement by run mode, "
            "the interim app-side posture is to soften the gate for self-deployed "
            "runs — e.g. preflight_gate_mode = 'soft' if ENABLE_ATLAN_UPLOAD else "
            "'hard' — keeping it env-overridable via ATLAN_PREFLIGHT_GATE_MODE."
        ),
        short_description=(
            "SDR app hard-codes preflight_gate_mode = 'hard' — non-secret-config "
            "preflight failures abort agent-mode workflows"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            'an unconditional ``preflight_gate_mode = "hard"`` assignment is\n'
            "flagged.\n"
            "\n"
            "The SDK's preflight gate (``App.preflight_gate_mode``) defaults to\n"
            '``"soft"`` — a NOT_READY verdict is logged (``would_block``) but the\n'
            'run proceeds.  An app opts into ``"hard"`` when its checks are\n'
            "trusted to gate real runs.  That trust assumption breaks in SDR\n"
            "(agent) mode: configuration is resolved through the *customer's*\n"
            "secret store, and customers rightly mirror only secrets into it —\n"
            "non-sensitive values (a database name for a schema-existence check,\n"
            "for example) are commonly absent.  The preflight check then fails for\n"
            "a reason unrelated to the run's actual viability, and a hard gate\n"
            "aborts the whole workflow (observed for a query-engine connector in\n"
            "fleet testing; connectors with soft gates ran the same checks\n"
            "non-fatally and succeeded).\n"
            "\n"
            "This is suggest-only (WARN): a hard gate is a legitimate posture for\n"
            "cloud-hosted runs, and the right long-term fix is SDK-side — gate\n"
            "enforcement differentiated by run mode, so the same app can be hard\n"
            "in cloud mode and soft in agent mode.  Until then:\n"
            "\n"
            "**Remediation (interim app-side posture):** derive the gate mode from\n"
            "the run mode instead of hard-coding it, e.g.::\n"
            "\n"
            '    preflight_gate_mode = "soft" if ENABLE_ATLAN_UPLOAD else "hard"\n'
            "\n"
            "and keep it env-overridable (the SDK's ``ATLAN_PREFLIGHT_GATE_MODE``\n"
            "env var takes precedence over the class attribute, so operators can\n"
            "restore the hard gate per deployment).  A conditional assignment like\n"
            'the above is not flagged — only an unconditional ``"hard"``.\n'
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p041"
        ),
    ),
)

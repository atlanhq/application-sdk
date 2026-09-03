"""SDR-readiness rule definitions (P029, P030, P037, P038, P039, P042, P051).

Apps that declare ``self_deployed_runtime: true`` in ``atlan.yaml`` must satisfy
seven structural invariants before they can be considered SDR-ready:

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
  A *working* hand-rolled bridge is P042's, not P030's.

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


* ``P042`` — an app whose custom ``upload_to_atlan`` bridge *does* transfer, with
  no ``self.upload()`` anywhere.  Split out of P030 because the failure is
  different in kind: nothing is silently dropped, but an SDK-owned contract has
  been reimplemented on a symbol scheduled for removal in v4.0, so the finding
  needs its own severity, its own remediation text, and a retirement date P030
  does not have.

* ``P051`` — an app whose ``uv.lock`` resolves ``atlan-application-sdk`` below
  ``3.30.0``, the floor at which the SDR interactive setup surfaces (test
  authentication, preflight checks, and metadata browsing — the ``sdr:*`` worker
  activities) become available.  heracles rejects interactive dispatch to a
  worker below the floor and the frontend hides the widgets, so onboarding the
  app in a self-deployed runtime offers none of them.  A readiness nudge rather
  than a silent-data-loss bug — hence WARN — and unlike the others it reads the
  locked dependency version, not app source or the manifest.

All rules are APP-scoped (the SDK itself does not declare ``self_deployed_runtime``
and is therefore always skipped) and gate on ``self_deployed_runtime: true``
being present in ``atlan.yaml``.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P029",
        canonical_reference=(
            "atlan-metabase-app app/generated/manifest.json — `agent_json` and "
            "`extraction_method` are top-level keys of $.dag.extract.inputs.args. Both are "
            "emitted by the toolkit renderer, so no app-side edit produces them; an app "
            "missing them needs a toolkit bump and a regenerate."
        ),
        rule_interactions=(
            "Not a toolkit-version gap: bumping an affected app to the newest toolkit "
            "and regenerating does NOT add the field. It comes from the renderer's "
            "per-widget emission."
        ),
        terminal_state=(
            "The toolkit emits agent_json defensively but historically not "
            "extraction_method, so an SDR app with no extraction-method widget is "
            "half-wired through no fault of its own. Fix the renderer; adding the "
            "widget app-side also switches the Self-Deployed Runtime option on in the "
            "form, which is a product decision rather than a conformance fix."
        ),
        fix_locus=FixLocus.TOOLKIT,
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
            "'success', invisible to status-only test pipelines. "
            "Customer impact: both failure modes land on the customer — an agent crawl "
            "that hangs against their firewalled source or completes green with zero "
            "assets in their catalog — and both look like a working product until the "
            "customer asks where their metadata went."
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
        canonical_reference=(
            "atlan-metabase-app app/connector.py — `run()` uploads each transformed "
            "typename with `raise_on_empty=True`, and uploads residual/ separately. An SDR "
            "app with no self.upload() call leaves the ENABLE_ATLAN_UPLOAD path "
            "unreachable, so the e2e leg greens without moving a byte to the tenant "
            "bucket."
        ),
        rule_interactions=(
            "The finding may anchor on generated output (app/generated/**), which is "
            "not editable — a hand-edit is erased by the next regeneration and turns "
            "the freshness gate red. Fix contract/*.pkl instead, then run the repo's "
            "OWN generate task: a bare `pkl eval` skips the post-processing step and "
            "rewrites unrelated generated files. Diff atlan.yaml afterwards, which "
            "regeneration can silently strip hand-written comments from."
        ),
        fix_locus=FixLocus.CONTRACT,
        scope=RuleScope.APP,
        name="SdrUploadNotCalled",
        tier=EnforcementTier.BLOCK,
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
            "presumed a false positive. "
            "Customer impact: this is the worst customer-facing failure class — data loss "
            "disguised as success. The tenant reports a green run while zero assets reach "
            "the customer's catalog, so it is the customer who discovers the gap, after "
            "trusting the green status for however long it took them to look."
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
            "A *working* hand-rolled ``upload_to_atlan`` bridge is reported by\n"
            "**P042**, not here: bytes do move, so it is not this rule's\n"
            "silent-zero-asset shape.  It is not silence either — see P042 for why\n"
            "a working bridge still has to change.\n"
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
            "This is a BLOCK: the failure it names is a silent zero-asset publish\n"
            "in a customer tenant, reported to the customer as a successful run.\n"
            "The two shapes that originally held it at WARN no longer do.\n"
            "Preflight-only apps are now exempted structurally by the\n"
            "``pipeline.publish = null`` carve-out below, not by the tier.  And\n"
            "delegating to a base-class ``upload`` defined in the SDK template does\n"
            "not clear the finding on purpose — an inherited ``upload`` that nothing\n"
            "ever calls is exactly the unreachable-gate shape, and the specific case\n"
            "of deferring to it explicitly (``super().upload(...)``) IS accepted as a\n"
            "real call.  Fix by adding ``await self.upload(...)`` to the ``run()``\n"
            "method or the relevant ``@entrypoint`` method.\n"
            "\n"
            "One residual false-positive shape remains, and it is a *stub* finding,\n"
            "not an absence finding: a custom ``upload_to_atlan`` bridge whose\n"
            "transfer happens inside a helper **inherited from a base class in\n"
            "another file** cannot be resolved by the checker and reads as a no-op\n"
            "stub (documented on ``_find_upload_bridges``).  Widening delegation to\n"
            "any ``self.x(...)`` would reopen the false negative the rule exists to\n"
            "close.\n"
            "\n"
            "**This rule honours no inline suppression.** The SDR checks build their\n"
            "``Finding`` objects directly and never parse ``# conformance: ignore``\n"
            "directives, and the absence finding is anchored at line 1 of\n"
            "``atlan.yaml`` where YAML has no comment the parser reads anyway.  At\n"
            "BLOCK that means the only exits are real ones: make the transfer\n"
            "visible where the checker can see it (call ``self.upload(...)``, or\n"
            "``super().upload(...)``, or keep the delegated helper in the same\n"
            "class), or declare the app publish-less via ``pipeline.publish = null``\n"
            "so the structural carve-out below applies.  Deliberately so — every\n"
            "shape on the not-satisfied list above was a real silent-zero-asset\n"
            "publish in fleet testing, and an easy opt-out is how this class stayed\n"
            "invisible.\n"
            "\n"
            "Note: P008 flags ``self.upload()`` *inside* ``@task`` methods (the\n"
            "wrong location); P030 flags the *absence* of any upload call; P042\n"
            "flags a hand-rolled bridge standing in for it.  All three should be\n"
            "clean for a correctly-wired SDR app.\n"
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
        canonical_reference=(
            "atlan-metabase-app app/credentials.py — `build_credential_ref` routes through "
            "`CredentialRef.resolve`, which covers direct (credential_guid) and agent "
            "(agent_json) modes from one call. Resolving by credential_guid alone works in "
            "direct mode and silently ignores agent_json in SDR mode."
        ),
        fix_locus=FixLocus.CONTRACT,
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
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — the upload's storage_path comes from "
            "`base_result.transformed_data_prefix`, which the SDK roots from "
            "APPLICATION_NAME. An input field named application_name defaults to empty, so "
            "rooting the prefix from it silently writes to the bucket root."
        ),
        rule_interactions=(
            "The finding may anchor on generated output (app/generated/**), which is "
            "not editable — a hand-edit is erased by the next regeneration and turns "
            "the freshness gate red. Fix contract/*.pkl instead, then run the repo's "
            "OWN generate task: a bare `pkl eval` skips the post-processing step and "
            "rewrites unrelated generated files. Diff atlan.yaml afterwards, which "
            "regeneration can silently strip hand-written comments from."
        ),
        fix_locus=FixLocus.CONTRACT,
        scope=RuleScope.APP,
        name="SdrArtifactMisrooted",
        tier=EnforcementTier.BLOCK,
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
            "document-store connector in fleet testing). "
            "Customer impact: the same data loss disguised as success that P030 "
            "polices, one seam later — the run goes green, the upload reports "
            "success, and zero assets reach the customer's catalog because the "
            "publish step reads a prefix nothing was ever written to."
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
            "This is a BLOCK, on the same grounds as P030: the failure is a silent\n"
            "zero-asset publish in a customer tenant, reported as a successful run.\n"
            "That it is not a hard crash is the reason it blocks rather than a reason\n"
            "it does not — a crash announces itself, this does not.\n"
            "\n"
            "The heuristic is deliberately narrow (it keys on the ``application_name``\n"
            "input field feeding an ``artifacts/apps`` literal), but narrow here means\n"
            "**under**-inclusive, not imprecise: it does NOT catch every mis-rooting —\n"
            "an app that forwards an empty ``output_prefix`` input field without an\n"
            "``artifacts/apps`` literal is indistinguishable from a correct app\n"
            "statically and is left to runtime/e2e detection.  A rule that misses\n"
            "cases can still block the cases it does name; the shape it flags is\n"
            "wrong on the wire, not a matter of taste.\n"
            "\n"
            "Like every SDR check, this rule honours no inline suppression (the SDR\n"
            "checks build their ``Finding`` objects directly and never parse\n"
            "``# conformance: ignore`` directives).  The exit is the remediation\n"
            "below, which is a one-line change to where the prefix is rooted.\n"
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
        canonical_reference=(
            "atlan-metabase-app app/contracts.py — `MetabaseInput` declares `agent_json` "
            "as a typed field, and atlan-metabase-app app/generated/_input.py extends the "
            "SDK's `ExtractionInput` rather than a bare `Input`. Either route keeps the "
            "forwarded value; a bare Input subclass with no agent_json field drops it "
            "before the credential resolver sees it."
        ),
        rule_interactions=(
            "The finding may anchor on generated output (app/generated/**), which is "
            "not editable — a hand-edit is erased by the next regeneration and turns "
            "the freshness gate red. Fix contract/*.pkl instead, then run the repo's "
            "OWN generate task: a bare `pkl eval` skips the post-processing step and "
            "rewrites unrelated generated files. Diff atlan.yaml afterwards, which "
            "regeneration can silently strip hand-written comments from."
        ),
        fix_locus=FixLocus.CONTRACT,
        scope=RuleScope.APP,
        name="SdrAgentJsonDroppedByInputContract",
        tier=EnforcementTier.BLOCK,
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
            "testing). "
            "Customer impact: in agent mode the extraction never receives the "
            "customer's credentials, so their crawl either fails outright or completes "
            "green with zero assets in their catalog — and because the manifest and "
            "the connector code both look correct, nothing on our side points at the "
            "cause until the customer asks where their metadata went."
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
            "This is a BLOCK: agent-mode extraction with no credentials is a\n"
            "zero-asset run in a customer tenant, and all three conditions the check\n"
            "requires must hold together before it fires — a bare ``Input`` base, no\n"
            "``agent_json`` field, and extra fields rejected.  Any one of them being\n"
            "false clears the finding.\n"
            "\n"
            "Residual imprecision, stated plainly: the heuristic reads the generated\n"
            "contract's *declared* bases, fields, and model config, so an app that\n"
            "receives ``agent_json`` through a custom intermediate base the heuristic\n"
            "does not resolve would be flagged.  The surface this runs against is\n"
            "toolkit-**generated** (``AppInputContract`` in a generated ``_input.py``),\n"
            "where that shape does not arise from the standard templates; and like\n"
            "every SDR check this rule honours no inline suppression, so the exits are\n"
            "the three remediations below.  Each of them is correct on its own merits\n"
            "whether or not the finding was precise: declaring ``agent_json`` on the\n"
            "contract is what makes the forwarded field part of the app's typed\n"
            "surface instead of something it happens to tolerate.\n"
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
        id="P042",
        canonical_reference=(
            "atlan-metabase-app app/connector.py — the tenant-bucket hand-off is `await "
            "self.upload(UploadInput(...))`. A hand-rolled upload_to_atlan bridge "
            "re-implements the routing to upstream_storage and then has to track it as the "
            "SDK changes."
        ),
        fix_locus=FixLocus.CONTRACT,
        scope=RuleScope.APP,
        name="SdrHandRolledUploadBridge",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        superseded_by="sdk>=4.0.0",
        rationale=(
            "An app that moves extracted assets to the tenant bucket through its own "
            "upload_to_atlan method, with no self.upload() anywhere, has "
            "reimplemented a contract the SDK owns — on a symbol the SDK has marked "
            "@deprecated with removal_version 4.0.0. Bytes do move, so this is not "
            "P030's silent-zero-asset shape and it should not carry P030's message; "
            "but it is not a false positive either. App.upload() does ADR-0014 "
            "dual-write routing, transformed-asset validation in a child process, the "
            "canonical artifacts/apps/{app}/workflows/{wf}/{run} prefix and @task "
            "retry/replay, and the transfer beneath it adds the cross-pod "
            "deployment-store fallback (a KEDA-scaled SDR worker where local_path "
            "does not exist on this pod), partial-local reconcile, and SHA-256 "
            "sidecar dedup for idempotent replay. A hand-rolled bridge has none of "
            "those, and a green full-DAG e2e proves only that bytes moved on that "
            "run, not that the app tracks the contract. Reporting it separately also "
            "settles a contradiction: B001 already flags .upload_to_atlan(...) call "
            "sites from the generated deprecation manifest, so the same repo would "
            "otherwise get a B001 finding alongside P-series silence."
        ),
        short_description=(
            "SDR app performs the tenant-bucket transfer through a hand-rolled "
            "upload_to_atlan bridge instead of App.upload()"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "this rule fires when a custom ``upload_to_atlan`` method **does**\n"
            "perform a real storage/store transfer (in its own body or via\n"
            "same-class delegation) and no ``self.upload(`` call exists anywhere in\n"
            "the app source.\n"
            "\n"
            "It is the counterpart to P030, which owns the shapes where nothing\n"
            "moves at all: no upload path, or an ``upload_to_atlan`` stub whose body\n"
            "performs no transfer.  Here the transfer works — which is exactly why\n"
            "it needs a different message.  Treating it as a P030 false positive\n"
            "moved these repos from WARN to silent, and silence is wrong for three\n"
            "reasons:\n"
            "\n"
            "* **It is the SDK's own deprecated symbol.**  ``upload_to_atlan`` is\n"
            "  ``@deprecated`` in ``application_sdk.templates.base_metadata_extractor``\n"
            "  with ``removal_version: 4.0.0``.  Going quiet on a locally\n"
            "  reimplemented version of a name scheduled for deletion is the\n"
            "  opposite of what the deprecation lifecycle is for.\n"
            "* **The suite would contradict itself.**  B001 matches\n"
            "  ``.upload_to_atlan(...)`` call sites receiver-agnostically from the\n"
            "  generated manifest.  Where the bridge is invoked explicitly rather\n"
            "  than only dispatched as a DAG task, the repo already gets a B001\n"
            "  finding — alongside P-series silence.\n"
            "* **A bridge cannot be equivalent, and the gap is silent-data-loss\n"
            "  shaped.**  ``App.upload()`` carries ADR-0014 dual-write routing,\n"
            "  transformed-asset validation in a child process, the canonical\n"
            "  ``artifacts/apps/{app}/workflows/{workflow_id}/{run_id}`` prefix, and\n"
            "  ``@task`` retry/replay semantics; the transfer underneath adds the\n"
            "  cross-pod deployment-store fallback (a KEDA-scaled SDR worker where\n"
            "  ``local_path`` does not exist on this pod), partial-local reconcile,\n"
            "  and SHA-256 sidecar dedup for idempotent replay.\n"
            "\n"
            "**A green full-DAG e2e is not a clearance.**  It shows bytes moved on\n"
            "that run.  It does not show the bridge preserves the key layout under\n"
            "replay, survives a pod that never held the local files, or reconciles a\n"
            "partial local state — and it says nothing about v4.0.\n"
            "\n"
            "**Remediation:** replace the bridge body with\n"
            "``await self.upload(...)`` in the ``run()`` method or the relevant\n"
            "``@entrypoint`` method (crawler AND miner), and delete the bridge.  If\n"
            "the bridge exists because ``App.upload()`` genuinely cannot express\n"
            "something the app needs, that is an SDK gap worth filing rather than a\n"
            "reason to suppress.\n"
            "\n"
            "This is a WARN, and deliberately a *lower*-urgency one than P030: the\n"
            "app is working today.  The deadline is v4.0, not the next run — which\n"
            "is why the rule carries ``superseded_by: sdk>=4.0.0``.  It retires when\n"
            "that removal lands and the shape stops being expressible.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p042"
        ),
    ),
    RuleDefinition(
        id="P051",
        canonical_reference=(
            "atlan-mysql-app uv.lock — the SDK resolves to 3.32.0, above the 3.30.0 floor "
            "that carries interactive setup (test auth, preflight, metadata browsing). The "
            "declared range in pyproject.toml is what lets the lock reach it."
        ),
        fix_locus=FixLocus.CONTRACT,
        scope=RuleScope.APP,
        name="SdrPreflightUnavailable",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="sdr-readiness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.25.0",
        rationale=(
            "The SDR interactive setup surfaces — test authentication, preflight "
            "checks, and metadata browsing (the sdr:* worker activities) — first "
            "ship in application-sdk 3.30.0. heracles rejects interactive dispatch "
            "to a worker below that floor (its BelowFloorError guard) and the "
            "frontend hides the widgets, so an app that declares "
            "self_deployed_runtime: true but locks application-sdk below 3.30.0 can "
            "offer none of the onboarding experience the customer expects. This rule "
            "reads the version actually locked in uv.lock (not the pyproject "
            "specifier) so it reflects what the deployed worker ships. It is a "
            "readiness nudge, not a broken-crawl or data-loss failure — an app below "
            "the floor still extracts and publishes normally, it only lacks the "
            "interactive setup UX — so it lands at WARN."
        ),
        short_description=(
            "SDR app locks application-sdk below the 3.30.0 floor for interactive "
            "setup (test auth / preflight / metadata browsing)"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "``uv.lock`` must resolve ``atlan-application-sdk`` to ``3.30.0`` or\n"
            "newer — the floor at which the SDR interactive setup surfaces become\n"
            "available on the worker:\n"
            "\n"
            "* **test authentication** (``sdr:test_auth``),\n"
            "* **preflight checks** (``sdr:preflight_check``), and\n"
            "* **metadata browsing** (``sdr:fetch_metadata``).\n"
            "\n"
            "Both sides of the platform gate on this floor. heracles rejects an\n"
            "interactive dispatch to a worker running an older application-sdk\n"
            "(``BelowFloorError`` → a 4xx with an upgrade message), and the frontend\n"
            "hides the interactive widgets unless the agent reports an\n"
            "``sdk_version`` at or above the floor. So an SDR app pinned lower can\n"
            "offer none of the onboarding experience — the customer sees no preflight\n"
            "result, no test-authentication, and a plain input in place of the\n"
            "include/exclude metadata tree.\n"
            "\n"
            "The check reads the **locked** version from ``uv.lock`` — the version\n"
            "the deployed worker actually ships — rather than the ``pyproject.toml``\n"
            "specifier, which may be a range that resolves higher.  A version that\n"
            "cannot be resolved (no ``uv.lock``, no ``atlan-application-sdk`` entry\n"
            "in it, or an unparseable lock) is left **silent**: it cannot be\n"
            "confirmed below the floor, and the D-series already governs a missing or\n"
            "unbounded SDK declaration.\n"
            "\n"
            "This is a WARN, not a BLOCK: an app below the floor still extracts and\n"
            "publishes assets normally — it is only missing the interactive setup UX,\n"
            "not losing data or failing runs.\n"
            "\n"
            "**Remediation:** raise the ``atlan-application-sdk`` pin to\n"
            "``>= 3.30.0`` in ``pyproject.toml`` and re-lock::\n"
            "\n"
            "    uv lock --upgrade-package atlan-application-sdk\n"
            "\n"
            "then rebuild and redeploy the SDR worker image so the deployed agent\n"
            "reports the new ``sdk_version``.  A merged bump alone does not enable the\n"
            "interactive surfaces — the platform reads the version off the running\n"
            "worker.\n"
            "\n"
            "**The floor is necessary but not sufficient.**  Clearing it only lets the\n"
            "platform *offer* each surface; they are switched on in the app's own\n"
            "contract (``contract/app.pkl`` → ``pkl eval`` to regenerate):\n"
            "\n"
            "* **Preflight checks** — declare a ``preflight-check`` widget\n"
            "  (``Config.SageV2`` with its ``checks {}``) in the entrypoint's form and\n"
            '  list ``"preflight-check"`` in that entrypoint\'s ``required`` UIRule.\n'
            "  It is per entrypoint: one that omits the widget runs no preflight (a\n"
            "  miner may leave it out deliberately).\n"
            "* **Test authentication** — set ``allowTestAuthentication = true`` on the\n"
            "  credential / agent widget in the entrypoint form. The toolkit emits it\n"
            "  as ``ui.showTestAuthentication`` in the generated manifest, and the\n"
            "  setup UI shows the Test Authentication button only when that flag is set\n"
            "  AND the agent clears this floor. Emitting it needs an\n"
            "  ``app-contract-toolkit`` version that supports\n"
            "  ``allowTestAuthentication``.\n"
            "* **Include / exclude metadata filters** — once the agent clears the floor\n"
            "  these render the interactive metadata picker; below it (or when the\n"
            "  version can't be read) the picker falls back to a plain text box. This\n"
            "  follows the same floor gate automatically — no per-connector change\n"
            "  beyond declaring the filter widget.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p051"
        ),
    ),
)

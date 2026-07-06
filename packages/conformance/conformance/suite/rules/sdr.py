"""SDR-readiness rule definitions (P029–P030, DISTR-752).

Apps that declare ``self_deployed_runtime: true`` in ``atlan.yaml`` must satisfy
two structural invariants before they can be considered SDR-ready:

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

Both rules are APP-scoped (the SDK itself does not declare ``self_deployed_runtime``
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
            "in a customer deployment."
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
            "This is a WARN (not BLOCK): some apps are legitimately preflight-only\n"
            "or delegate upload to a base-class method defined in the SDK template.\n"
            "Review the finding before suppressing — if the app genuinely performs\n"
            "an extract-and-upload cycle, add ``await self.upload(...)`` to the\n"
            "``run()`` method or the relevant ``@entrypoint`` method.\n"
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
)

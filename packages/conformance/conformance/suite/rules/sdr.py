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
  the SDR test pipeline validated only workflow *status*, not output.

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
            "In SDR (agent) mode the platform routes credentials through the "
            "agent_json field in dag.extract.inputs.args of the connector manifest. "
            "A manifest that omits this field causes the SDR worker to start and the "
            "workflow to complete with status 'success', but the extraction agent "
            "receives no credentials and produces zero assets — a silent failure "
            "invisible to status-only test pipelines. This was the root cause of "
            "the MSSQL connector regression (atlan-mssql-app#177, DISTR-752)."
        ),
        short_description=(
            "SDR app manifest.json is missing agent_json in dag.extract.inputs.args"
        ),
        full_description=(
            "For apps declaring ``self_deployed_runtime: true`` in ``atlan.yaml``,\n"
            "every ``manifest.json`` under ``app/generated/`` must contain an\n"
            "``agent_json`` key inside ``dag.extract.inputs.args``.\n"
            "\n"
            "In SDR (agent) mode the platform (Heracles/AE) fills ``agent_json``\n"
            "at dispatch time with the credential-routing spec that tells the\n"
            "extraction worker how to resolve credentials from the customer's\n"
            "secret store.  A manifest that does not declare this slot cannot\n"
            "receive the value — the workflow starts, runs to completion, and\n"
            "reports success while the extraction agent silently operates with\n"
            "no credentials and writes zero assets.\n"
            "\n"
            "The MSSQL regression (atlan-mssql-app#177) was caused by exactly\n"
            "this: the manifest had no ``agent_json`` slot, the hand-written SDR\n"
            "test supplied the value directly (bypassing the manifest), so the\n"
            "test passed while production runs failed silently.  Adopting\n"
            "``BaseSDRIntegrationTest.manifest_path`` (T003) closes the test\n"
            "gap; this rule closes the static gap.\n"
            "\n"
            "**Remediation:** add the ``agent_json`` slot to the app's\n"
            "``contract/app.pkl`` extract inputs and re-run ``pkl eval`` to\n"
            "regenerate ``app/generated/<name>/manifest.json``.  Do not\n"
            "hand-edit the generated manifest — C002 tracks drift.\n"
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
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p030"
        ),
    ),
)

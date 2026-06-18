"""CI/workflow supply-chain rule definitions (C-series)."""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="C001",
        name="UnpinnedActionReference",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="supply-chain",
        autofixable=True,
        since="3.16.0",
        rationale=(
            "A mutable tag (@v4) can be silently re-pointed to any commit after review — "
            "including malicious code — with no notification to the consumer. Pinning to a "
            "full commit SHA makes the action content immutable: the code reviewed is the "
            "code that runs."
        ),
        short_description="External GitHub Action not pinned to a full commit digest",
        full_description=(
            "External actions reused via `uses:` must be pinned to a full-length "
            "commit SHA (digest), never a mutable tag (@v4) or branch (@main). A "
            "tag can be re-pointed to malicious code after review. Actions in the "
            "`atlanhq/` org are exempt (they intentionally track @main); local "
            "`./` composite-action refs are exempt (no version to pin)."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/ci.md#c001",
    ),
    RuleDefinition(
        id="C002",
        name="BootstrapWorkflowDrift",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="ci-consistency",
        autofixable=True,
        since="0.3.0",
        rationale=(
            "Managed CI workflows enforce fleet-wide guarantees: uniform security scanning, "
            "consistent release gating, current conformance checks. Drift means an app runs "
            "an older workflow that may lack a recently-added security gate or use a "
            "deprecated step — invisible until exploited or until the step fails."
        ),
        short_description="Managed CI workflow is absent or has drifted from the bootstrap canonical",
        full_description=(
            "The `atlan-application-sdk-conformance bootstrap` command installs a "
            "standard set of CI workflow shims into `.github/workflows/`. This rule "
            "flags any managed file that is missing or whose content has diverged "
            "from what `bootstrap` would write. Re-run `bootstrap` to re-sync; "
            "structural drift is flagged while intentional per-repo value choices "
            "(e.g. `package_name`, `unit_tests_workflow_file`) are preserved."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/ci.md#c002",
    ),
    RuleDefinition(
        id="C003",
        name="GitignoreMissingEntry",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="ci-consistency",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "A .gitignore that is missing standard entries risks accidentally committing "
            "secrets, virtual environments, build artefacts, or IDE noise — each of which "
            "has caused incidents or review friction. The standard set is the minimal "
            "baseline every app repo should carry."
        ),
        short_description=".gitignore is absent or missing a standard required entry",
        full_description=(
            "The `atlan-application-sdk-conformance bootstrap` command scaffolds a "
            "standard .gitignore when the file is absent. This rule flags any required "
            "entry that is missing from the file. One finding is emitted per missing "
            "entry so each can be triaged or suppressed independently. "
            "Equivalences are respected: `.venv` covers `.venv/`, and "
            "`**/node_modules/**` covers `node_modules/`. "
            "Both absent-file and missing-entry findings are WARN only — the file is "
            "app-editable and must never block CI."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/ci.md#c003",
    ),
)

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
)

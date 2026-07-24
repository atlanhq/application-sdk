"""M-series — customer-facing metadata rules (BLDX-1575).

The first *model-driven* rule series.  Apps declare customer-visible metadata in
``atlan.yaml`` (generated from ``contract/app.pkl``); internal jargon or
off-convention names there leak straight into demos and customer tenants.
Recognising these is a judgement call, not a pattern, so the verdict comes from a
pinned language model (``mechanism=model``) rather than AST/regex.

Posture (see the design doc / plan): model rules are **advisory** — ``tier=WARN``
— so they never block CI on a non-deterministic verdict, and every rule pins its
``model_id`` and ``prompt_version`` so a finding is attributable and auditable
even though it cannot be perfectly reproduced.  ``orthogonal_gate`` is ``None``:
metadata fixes are customer-facing copy that no deterministic gate can confirm,
so remediation is suggest-only (routed to the residue report for human sign-off),
never auto-verified.
"""

from __future__ import annotations

from conformance.suite.checks._model_common import MODEL_ID
from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HELP_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/"
    "packages/conformance/conformance/docs/rules/metadata.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="M001",
        scope=RuleScope.APP,
        name="AppNamingConvention",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.MODEL,
        category="customer-facing-metadata",
        autofixable=False,
        since="0.18.0",
        model_id=MODEL_ID,
        prompt_version="m001-v1",
        rationale=(
            "App and entrypoint names render in the marketplace and in customer "
            "tenants. The fleet convention is to name an app after the system or "
            "technology it integrates with, not the vendor/company that owns it "
            "(e.g. 'Redshift Miner', not 'Amazon Redshift Miner'). Names drift "
            "from this inconsistently across the fleet (BLDX-1575). Recognising a "
            "company/vendor proper noun is inherently fuzzy and cannot rely on a "
            "committed name list in a public repo, so the check is model-driven."
        ),
        short_description=(
            "App or entrypoint name embeds a vendor/company prefix against "
            "convention"
        ),
        full_description=(
            "The customer-facing ``name`` / ``display_name`` in ``atlan.yaml`` "
            "(and per-entrypoint names) should follow the fleet convention of "
            "naming an app after the system/technology it integrates with, not "
            "the vendor or company that owns it.\n"
            "\n"
            "A pinned language model judges each name against that convention "
            "(no committed company-name list — that must not live in a public "
            "repo). This is an **advisory** (``warn``) rule: a flagged name is "
            "surfaced for review, never a merge blocker.\n"
            "\n"
            "Suppress with ``# conformance: ignore[M001] <reason>`` on the "
            "``name:`` line in ``atlan.yaml`` (or the comment-only line directly "
            "above it) when a name is deliberate."
        ),
        help_uri=f"{_HELP_BASE}#m001",
    ),
    RuleDefinition(
        id="M002",
        scope=RuleScope.APP,
        name="InternalJargonInDescription",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.MODEL,
        category="customer-facing-metadata",
        autofixable=False,
        since="0.18.0",
        model_id=MODEL_ID,
        prompt_version="m002-v1",
        rationale=(
            "Customer-facing description fields (short_description, "
            "long_description, entrypoint description) render in the marketplace, "
            "in demos, and in customer tenants. Internal jargon, codenames, or "
            "developer placeholder text there leaks internal wording to customers "
            "(BLDX-1575, observed on a connector description). Whether a phrase is "
            "internal jargon is a judgement, so the check is model-driven."
        ),
        short_description=(
            "Customer-facing description contains internal jargon or "
            "non-customer-appropriate language"
        ),
        full_description=(
            "The ``short_description`` / ``long_description`` in ``atlan.yaml`` "
            "and each entrypoint ``description`` ship to customers. A pinned "
            "language model flags copy that contains internal jargon, internal "
            "codenames/team/project names, ticket identifiers, internal tooling "
            "references, or developer placeholder text. Ordinary product and "
            "technical vocabulary is not flagged.\n"
            "\n"
            "This is an **advisory** (``warn``) rule: a flagged description is "
            "surfaced for review, never a merge blocker. The finding carries the "
            "exact flagged span in ``atlan/evidence``.\n"
            "\n"
            "Suppress with ``# conformance: ignore[M002] <reason>`` on the "
            "description's key line in ``atlan.yaml`` (or the comment-only line "
            "directly above it) once a description has been reviewed and accepted."
        ),
        help_uri=f"{_HELP_BASE}#m002",
    ),
)

"""Prescription rule definitions (P-series).

Above-the-bar engineering prescriptions the SDK mandates that do not fall under
a narrower category series (error-handling E, logging L, CI C, security S,
backwards-compatibility B, tests T, automation A).  Optimisations and
recommendations *below* the prescription bar live in the O-series instead.

Rule-id stability (non-migration policy)
----------------------------------------
P-ids are a permanent public contract: each is exposed in SARIF ``help_uri`` and
referenced by inline ``# conformance: ignore[Pxxx]`` suppressions across the
fleet.  A P-id therefore **never migrates and never changes**, even if a future
domain series (S/B/T/A/…) later subsumes the same topic.  When a domain series
takes over an area, the P-rule is retired in place (kept documented, no longer
firing) and the new rule gets a fresh id — the original P-id is never reused or
reassigned.  The same policy applies to O-ids.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P001",
        name="UnboundedContractFields",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-payload-safety",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        short_description="Input/Output contract declared with allow_unbounded_fields=True — opts out of payload safety",
        full_description=(
            "An ``Input``/``Output`` contract subclass declared with the\n"
            "``allow_unbounded_fields=True`` class keyword opts out of the SDK's\n"
            "payload-safety enforcement: arbitrary, untyped fields may cross task\n"
            "boundaries unchecked.  This is intended to be *extremely* exceptional —\n"
            "the sanctioned way to use it is an inline, justified suppression at the\n"
            "declaration site (``# conformance: ignore[P001] <reason>``), so the\n"
            "carve-out is reviewed and stays visible.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report (counted in\n"
            "their own category), so every opt-out is reported every single time.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed declaration fails\n"
            "the conformance gate — the only sanctioned use is the justified inline\n"
            "suppression above — see BLDX-1428.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p001",
    ),
    RuleDefinition(
        id="P002",
        name="CategoryFieldOverride",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="category-immutability",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        short_description="AppError subclass redeclares the `category` ClassVar — drifts the canonical taxonomy",
        full_description=(
            "``FailureCategory`` is the closed, single-axis taxonomy the SDK owns —\n"
            "every value is the canonical answer to *what happened* and is consumed as\n"
            "an immutable reporting metric (dashboards, SLA gates, on-call routing).\n"
            "The 14 categorical leaves in ``application_sdk.errors.leaves`` (and\n"
            "``AppError`` itself) are the sole defining sites: each leaf binds exactly\n"
            "one ``FailureCategory`` to its ``category`` ``ClassVar``.\n"
            "\n"
            "Domain subclasses MUST inherit ``category`` from their categorical-leaf\n"
            "parent — never redeclare it.  A redeclaration is either a same-value\n"
            "duplication (clutter that drifts as soon as the parent is renamed) or a\n"
            "true override (silent taxonomy drift that splits a metric across\n"
            "lookalike values).  Both are blocked uniformly: domain subclasses\n"
            "specialise via ``code`` and evidence fields, not ``category``.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report (counted in\n"
            "their own category), so every opt-out is reported every single time.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed redeclaration\n"
            "fails the conformance gate — the only sanctioned use is the justified\n"
            "inline suppression ``# conformance: ignore[P002] <reason>`` at the\n"
            "declaration site — see BLDX-1432.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p002",
    ),
    RuleDefinition(
        id="P003",
        name="ErrorCodePrefixMismatch",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="error-code-shape",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        short_description="AppError subclass code missing or doesn't start with the parent leaf's category prefix",
        full_description=(
            "Every concrete subclass of an ``application_sdk.errors`` leaf "
            "(``AuthError``, ``InternalError``, ``InvalidInputError``, etc.) must\n"
            "declare its own ``code: ClassVar[str]`` that starts with the leaf's\n"
            "category prefix and an underscore (``AUTH_``, ``INTERNAL_``,\n"
            "``INVALID_INPUT_``, etc.).  Without that prefix the code is opaque to\n"
            "dashboards and on-call routing — the category column has to be joined\n"
            "for every query.  Without an override, every site of the subclass\n"
            "collapses into the leaf's bare bucket and is impossible to triage.\n"
            "\n"
            "The check resolves inheritance transitively: an intermediate class with\n"
            "no ``code`` (a 'pass-through' subclass between a leaf and a concrete\n"
            "leaf) is also flagged so failures don't silently inherit the bare\n"
            "leaf's code.  Suppress with ``# conformance: ignore[P003] <reason>``\n"
            "at the declaration when an intermediate is genuinely abstract — see\n"
            "typed-error-prescription §4 and BLDX-1431.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p003",
    ),
)

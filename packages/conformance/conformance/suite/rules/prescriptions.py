"""Prescription rule definitions (P-series).

Above-the-bar engineering prescriptions the SDK mandates that do not fall under
a narrower category series (error-handling E, logging L, CI C, security S,
backwards-compatibility B, tests T, automation A).  Optimisations and
recommendations *below* the prescription bar live in the O-series instead.
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

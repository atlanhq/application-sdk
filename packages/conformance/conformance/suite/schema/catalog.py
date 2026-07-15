"""Rule catalog — typed Python rule definitions and validation helpers.

``RuleDefinition`` is the source-of-truth form for every rule the
conformance suite knows about.  Concrete rule instances live in the
per-series modules under ``suite.rules``; this module only provides the
model and a validation helper.

Rule ID namespaces:

* ``E###``  — error-handling patterns (E001–E099)
* ``L###``  — logging patterns (L001–L099)
* ``C###``  — CI/workflow supply-chain patterns (C001–C099)
* ``D###``  — dependency patterns (D001–D099)
* ``I###``  — container image conformance patterns (I001–I099)
* ``T###``  — test-quality patterns (T001–T099)
"""

from __future__ import annotations

from typing import Any, Literal

from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)
from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Typed rule definition
# ---------------------------------------------------------------------------


class RuleDefinition(BaseModel):
    """A single rule definition.

    This is the *source-of-truth* form — ``to_reporting_descriptor()`` converts
    it to the SARIF wire form.
    """

    id: str = Field(..., pattern=r"^[A-Z]\d{3}$")
    """Stable rule ID, e.g. ``"P001"``, ``"L001"``, ``"C001"``."""

    name: str
    """CamelCase name, e.g. ``"BareExceptPass"``."""

    tier: EnforcementTier
    """``warn`` or ``block``."""

    mechanism: RuleMechanism
    """``static`` or ``test``."""

    scope: RuleScope
    """Where the rule applies — ``sdk``, ``app``, or ``both``.  Required (no
    default) so every rule must declare its surface explicitly; the meta-test
    ``test_catalog_all_have_scope`` enforces this for present and future rules."""

    category: str
    """Rule family, e.g. ``"silent-swallow"``."""

    autofixable: bool = False
    short_description: str = ""
    full_description: str = ""
    help_uri: str | None = None
    orthogonal_gate: Literal["tests", "pkl-eval", "skip"] | None = None
    """Named gate to run after a source-code fix.  ``None`` or ``"tests"`` runs
    the repository's standard test suite; ``"pkl-eval"`` runs the pkl-eval gate;
    ``"skip"`` skips gating entirely — for fixes that cannot affect Python or
    contract behaviour (e.g. a deterministic re-sync of a managed CI/scaffold
    file) so running the test suite would only add cost with no signal.
    Named ``"skip"`` rather than a bare ``"none"`` string so it cannot be
    confused with this field's own ``None`` default, which means the opposite
    thing (run the standard test suite).
    The field is an exhaustive ``Literal`` so that a typo (e.g. ``"pkleval"``)
    is caught at rule-definition time rather than silently falling through to the
    wrong gate at remediation time."""
    since: str | None = None
    """Conformance suite version when the behaviour behind this rule was first enforced.
    Tracks the behavioural appearance, not when a specific rule ID was assigned —
    so a renumbered rule retains the original ``since`` of the behaviour it
    describes, e.g. ``"0.2.0"``."""
    forces_external_influence: bool = False
    """``True`` if every fix for this rule must be treated as having consulted
    untrusted external content, regardless of what an individual remediation
    attempt reports. Structural counterpart to a fix's own (model-reported)
    ``external_influence`` result field: the model is trusted to set that
    field correctly on every invocation, but a rule known ahead of time to
    always involve an external lookup (e.g. C001's live GitHub SHA
    resolution) should not depend on the model remembering to do so every
    single time. ``detect-fix-recheck`` ORs this into its residue-routing
    condition alongside the model's own ``external_influence`` report, the
    same way ``orthogonal_gate`` is a structural (not model-reported) field."""
    rationale: str = ""
    """Why this rule exists — what risk it avoids, what loop it closes, or what
    value it adds. Surfaced as ``atlan/rationale`` in SARIF ``properties``."""

    @model_validator(mode="before")
    @classmethod
    def _normalise_enums(cls, data: Any) -> Any:
        """Accept string values for enum fields."""
        if isinstance(data, dict):
            if "tier" in data and isinstance(data["tier"], str):
                data["tier"] = EnforcementTier(data["tier"].lower())
            if "mechanism" in data and isinstance(data["mechanism"], str):
                data["mechanism"] = RuleMechanism(data["mechanism"].lower())
            if "scope" in data and isinstance(data["scope"], str):
                data["scope"] = RuleScope(data["scope"].lower())
        return data

    def to_reporting_descriptor(self) -> ReportingDescriptor:  # type: ignore[name-defined]  # noqa: F821
        """Return the SARIF ``ReportingDescriptor`` wire form for this rule."""
        from conformance.suite.schema.extensions import AtlanRuleProperties
        from conformance.suite.schema.sarif import (
            ReportingConfiguration,
            ReportingDescriptor,
        )

        rule_props = AtlanRuleProperties(
            tier=self.tier,
            mechanism=self.mechanism,
            scope=self.scope,
            category=self.category,
            autofixable=self.autofixable,
            orthogonal_gate=self.orthogonal_gate,
            since=self.since,
            rationale=self.rationale or None,
            forces_external_influence=self.forces_external_influence,
        )
        return ReportingDescriptor(
            id=self.id,
            name=self.name,
            short_description={"text": self.short_description}
            if self.short_description
            else None,
            full_description={"text": self.full_description}
            if self.full_description
            else None,
            help_uri=self.help_uri,
            default_configuration=ReportingConfiguration(
                level=self.tier.to_sarif_level(),
                enabled=True,
            ),
            properties=rule_props.to_properties(),
        )


# ---------------------------------------------------------------------------
# Catalog validation helper
# ---------------------------------------------------------------------------


def validate_catalog(rules: list[RuleDefinition]) -> None:
    """Validate a list of rule definitions for uniqueness.

    Raises
    ------
    ValueError
        If any rule ID is duplicated.
    """
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ValueError(f"duplicate rule ID: {rule.id!r}")
        seen.add(rule.id)

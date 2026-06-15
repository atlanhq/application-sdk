"""Rule catalog — typed Python rule definitions and validation helpers.

``RuleDefinition`` is the source-of-truth form for every rule the
conformance suite knows about.  Concrete rule instances live in the
per-series modules under ``suite.rules``; this module only provides the
model and a validation helper.

Rule ID namespaces:

* ``E###``  — error-handling patterns (E001–E099)
* ``L###``  — logging patterns (L001–L099)
* ``C###``  — CI/workflow supply-chain patterns (C001–C099)
* (reserved) ``T###`` — test-quality patterns
* (reserved) ``D###`` — dependency patterns
"""

from __future__ import annotations

from typing import Any

from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism
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

    category: str
    """Rule family, e.g. ``"silent-swallow"``."""

    autofixable: bool = False
    short_description: str = ""
    full_description: str = ""
    help_uri: str | None = None
    orthogonal_gate: str | None = None
    since: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_enums(cls, data: Any) -> Any:
        """Accept string values for enum fields."""
        if isinstance(data, dict):
            if "tier" in data and isinstance(data["tier"], str):
                data["tier"] = EnforcementTier(data["tier"].lower())
            if "mechanism" in data and isinstance(data["mechanism"], str):
                data["mechanism"] = RuleMechanism(data["mechanism"].lower())
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
            category=self.category,
            autofixable=self.autofixable,
            orthogonal_gate=self.orthogonal_gate,
            since=self.since,
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

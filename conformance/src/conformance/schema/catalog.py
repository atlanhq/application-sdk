"""Rule catalog — load and validate the rules-as-data YAML.

The canonical rule definitions live in
``conformance/src/conformance/rules/catalog.yaml``.  This module loads them,
validates them, and exposes ``RuleDefinition`` (the typed Python form) and the
SARIF ``ReportingDescriptor`` derived from each entry.

Rule IDs are allocated per category:

* ``P###``  — error-recovery patterns (P001–P099)
* ``L###``  — logging patterns (L001–L099)
* (reserved) ``T###`` — test-quality patterns
* (reserved) ``D###`` — dependency patterns

Catalog file format (YAML list of rule objects)::

    - id: P001
      name: BareExceptPass
      tier: block
      mechanism: static
      category: silent-swallow
      autofixable: false
      short_description: "Bare 'except: pass' — never acceptable"
      full_description: |
        A bare ``except: pass`` silently discards every exception including
        KeyboardInterrupt and SystemExit.  Replace with a typed catch that
        at minimum logs the error with ``exc_info=True``.
      help_uri: "https://example.atlan.com/conformance/rules/P001"
      orthogonal_gate: "tests"
      since: "3.16.0"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from conformance.schema.disposition import EnforcementTier, RuleMechanism

# ---------------------------------------------------------------------------
# Typed rule definition
# ---------------------------------------------------------------------------


class RuleDefinition(BaseModel):
    """A single rule entry from ``catalog.yaml``.

    This is the *source-of-truth* form — ``to_reporting_descriptor()`` converts
    it to the SARIF wire form.
    """

    id: str = Field(..., pattern=r"^[A-Z]\d{3}$")
    """Stable rule ID, e.g. ``"P001"``, ``"L001"``."""

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
        """Accept string values for enum fields (from YAML)."""
        if isinstance(data, dict):
            if "tier" in data and isinstance(data["tier"], str):
                data["tier"] = EnforcementTier(data["tier"].lower())
            if "mechanism" in data and isinstance(data["mechanism"], str):
                data["mechanism"] = RuleMechanism(data["mechanism"].lower())
        return data

    def to_reporting_descriptor(self) -> ReportingDescriptor:  # type: ignore[name-defined]  # noqa: F821
        """Return the SARIF ``ReportingDescriptor`` wire form for this rule."""
        from conformance.schema.extensions import AtlanRuleProperties
        from conformance.schema.sarif import ReportingConfiguration, ReportingDescriptor

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
# Catalog loader
# ---------------------------------------------------------------------------

_CATALOG_PATH = Path(__file__).parent.parent / "rules" / "catalog.yaml"


def load_catalog(path: Path | None = None) -> list[RuleDefinition]:
    """Load and validate all rule definitions from the catalog YAML.

    Parameters
    ----------
    path:
        Override the catalog file path.  Defaults to the bundled
        ``conformance/src/conformance/rules/catalog.yaml``.

    Returns
    -------
    list[RuleDefinition]
        Parsed, validated rule definitions in catalog order.

    Raises
    ------
    ValueError
        If any rule entry fails validation or duplicate IDs are found.
    FileNotFoundError
        If the catalog file does not exist at the resolved path.
    """
    resolved = path or _CATALOG_PATH
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))

    if not isinstance(raw, list):
        raise ValueError(
            f"Catalog at {resolved} must be a YAML list of rule objects, got {type(raw)}"
        )

    rules: list[RuleDefinition] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        rule = RuleDefinition.model_validate(entry)
        if rule.id in seen_ids:
            raise ValueError(f"Duplicate rule ID {rule.id!r} at catalog index {i}")
        seen_ids.add(rule.id)
        rules.append(rule)

    return rules

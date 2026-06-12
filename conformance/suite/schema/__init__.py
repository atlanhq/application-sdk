"""Conformance schema public API.

Re-exports the symbols every rule-checker and consumer needs so that downstream
code only needs to import from ``suite.schema``, not from the individual
sub-modules.
"""

from suite.schema.builder import ReportBuilder
from suite.schema.catalog import RuleDefinition, validate_catalog
from suite.schema.disposition import Disposition, derive_disposition
from suite.schema.extensions import (
    AtlanProperties,
    AtlanResultProperties,
    AtlanRuleProperties,
    AtlanRunProperties,
)
from suite.schema.findings import Finding, findings_to_report
from suite.schema.sarif import (
    Location,
    PhysicalLocation,
    Region,
    ReportingDescriptor,
    Result,
    SarifReport,
    SarifRun,
    Suppression,
)
from suite.schema.validate import validate_sarif

# load_catalog and get_rule delegate to suite.rules to avoid circular imports
# at module load time. We import lazily via functions so the rules package
# can import suite.schema without a circular dependency.


def load_catalog() -> list[RuleDefinition]:
    """Return all rules in definition order."""
    from suite.rules import load_catalog as _load

    return _load()


def get_rule(rule_id: str) -> RuleDefinition:
    """O(1) lookup by rule ID. Raises KeyError if not found."""
    from suite.rules import get_rule as _get

    return _get(rule_id)


__all__ = [
    # Disposition
    "Disposition",
    "derive_disposition",
    # Catalog
    "RuleDefinition",
    "validate_catalog",
    "load_catalog",
    "get_rule",
    # SARIF models
    "AtlanProperties",
    "AtlanResultProperties",
    "AtlanRunProperties",
    "AtlanRuleProperties",
    "Location",
    "PhysicalLocation",
    "Region",
    "ReportingDescriptor",
    "Result",
    "SarifRun",
    "SarifReport",
    "Suppression",
    # Builder
    "ReportBuilder",
    # Findings
    "Finding",
    "findings_to_report",
    # Validation
    "validate_sarif",
]

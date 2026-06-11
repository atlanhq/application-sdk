"""Conformance schema public API.

Re-exports the symbols every rule-checker and consumer needs so that downstream
code only needs to import from ``conformance.schema``, not from the individual
sub-modules.
"""

from conformance.schema.builder import ReportBuilder
from conformance.schema.catalog import RuleDefinition, load_catalog
from conformance.schema.disposition import Disposition, derive_disposition
from conformance.schema.extensions import (
    AtlanProperties,
    AtlanResultProperties,
    AtlanRuleProperties,
    AtlanRunProperties,
)
from conformance.schema.sarif import (
    Location,
    PhysicalLocation,
    Region,
    ReportingDescriptor,
    Result,
    SarifReport,
    SarifRun,
    Suppression,
)
from conformance.schema.validate import validate_sarif

__all__ = [
    # Disposition
    "Disposition",
    "derive_disposition",
    # Catalog
    "RuleDefinition",
    "load_catalog",
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
    # Validation
    "validate_sarif",
]

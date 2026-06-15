"""Thin Pydantic models for the SARIF 2.1.0 fields we populate.

We only model the subset of SARIF that the Atlan conformance suite emits —
not the full 2.1.0 spec.  The vendored ``sarif-schema-2.1.0.json`` is the
validation contract; ``validate_sarif()`` (``validate.py``) checks any
produced document against it.

Design notes
------------
* All ``Optional`` fields default to ``None`` / ``[]`` so builders can be
  additive without touching unused SARIF concepts.
* ``properties`` bags use ``dict[str, Any]`` — they are validated at a higher
  level by the typed ``AtlanRuleProperties`` / ``AtlanResultProperties``
  helpers in ``extensions.py``.
* Enum values are the exact lowercase strings SARIF 2.1.0 requires.
* ``model_config`` uses ``alias_generator=to_camel`` so all snake_case fields
  serialize to camelCase SARIF wire names automatically.  ``populate_by_name``
  lets ``model_validate`` accept either name.  Constructors use Python names.
* ``SarifReport.schema_`` uses an explicit ``serialization_alias="$schema"``
  because the ``$`` prefix cannot be derived from ``to_camel``.

SARIF reference: https://docs.oasis-open.org/sarif/sarif/v2.1.0/
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic.alias_generators import to_camel

_SARIF_SCHEMA_URL = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"

_SARIF_COMMON = ConfigDict(populate_by_name=True, alias_generator=to_camel)

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


class Region(BaseModel):
    """A region within an artifact (file)."""

    start_line: int = Field(..., ge=1)
    start_column: int = Field(1, ge=1)
    end_line: int | None = None
    end_column: int | None = None
    snippet: dict[str, str] | None = None
    """Optional ``{text: "..."}`` snippet for context."""

    model_config = _SARIF_COMMON


class ArtifactLocation(BaseModel):
    """Reference to a file artifact."""

    uri: str
    """Relative or absolute URI.  Use repo-root-relative paths for portability."""

    uri_base_id: str | None = None
    """Symbolic root, e.g. ``%SRCROOT%``."""

    model_config = _SARIF_COMMON


class PhysicalLocation(BaseModel):
    """A physical location within a source file."""

    artifact_location: ArtifactLocation
    region: Region | None = None

    model_config = _SARIF_COMMON


class Location(BaseModel):
    """A location associated with a result."""

    physical_location: PhysicalLocation | None = None
    message: dict[str, str] | None = None
    """Optional ``{text: "..."}`` message attached to the location."""

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


class Suppression(BaseModel):
    """A suppression record on a result.

    ``kind`` must be one of the SARIF-defined values:

    * ``"inSource"``  — justified annotation in the monitored source code
      (the primary suppression mechanism — lives next to the code, surfaces
      in review).
    * ``"external"``  — central allowlist (reserved; mirrors
      ``.security/base-allowlist.json`` style when needed).
    """

    kind: str = "inSource"
    """``"inSource"`` or ``"external"``."""

    justification: str = ""
    """Human-readable reason the suppression is warranted."""

    location: Location | None = None
    """The annotation site within the source file (for ``inSource``)."""

    status: str | None = None
    """Optional lifecycle status; ``"accepted"`` once reviewed."""


# ---------------------------------------------------------------------------
# Fix (proposed edit)
# ---------------------------------------------------------------------------


class Replacement(BaseModel):
    """A single text replacement within a file."""

    deleted_region: Region
    inserted_content: dict[str, str] | None = None
    """``{text: "replacement text"}``."""

    model_config = _SARIF_COMMON


class ArtifactChange(BaseModel):
    """Proposed changes to a single artifact."""

    artifact_location: ArtifactLocation
    replacements: list[Replacement] = []

    model_config = _SARIF_COMMON


class Fix(BaseModel):
    """A proposed fix for a result."""

    description: dict[str, str] | None = None
    """``{text: "..."}`` description of what the fix does."""

    artifact_changes: list[ArtifactChange] = []

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class Result(BaseModel):
    """A single conformance check finding.

    The three-state disposition is *derived* from these fields by
    ``derive_disposition()`` — it is not stored directly:

    * ``kind="pass"``                          → PASS
    * ``kind="fail"``, ``level="error"``,
      ``suppressions=[]``                      → FAILING  (blocks gate)
    * ``kind="fail"``, ``level="warning"``,
      ``suppressions=[]``                      → WARNING   (counted, non-blocking)
    * ``kind="fail"``, ``suppressions`` non-empty → SUPPRESSED (own category)
    """

    rule_id: str
    """Must match a ``reportingDescriptor.id`` in ``tool.driver.rules``."""

    rule_index: int | None = None
    """Zero-based index into ``tool.driver.rules`` for fast lookup."""

    kind: str = "fail"
    """``"pass"`` | ``"fail"`` | ``"open"`` | ``"review"`` | ``"notApplicable"``."""

    level: str | None = None
    """``"error"`` | ``"warning"`` | ``"note"`` | ``"none"``.
    If ``None``, the effective level is inherited from the rule's
    ``defaultConfiguration.level``."""

    message: dict[str, str] = Field(default_factory=lambda: {"text": ""})
    """``{text: "human-readable description of this specific occurrence"}``."""

    locations: list[Location] = []
    """File+line locations; typically one entry per result."""

    partial_fingerprints: dict[str, str] = Field(default_factory=dict)
    """Stable identity hashes for deduplication and oscillation detection.
    Key ``"atlanConformance/v1"`` = ``hash(ruleId + normalised_uri + startLine)``."""

    suppressions: list[Suppression] = []
    """Non-empty ↔ this result is intentionally suppressed."""

    fixes: list[Fix] = []
    """Proposed machine-actionable edits (populated by the remediation layer)."""

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/*`` extension properties; see ``AtlanResultProperties``."""

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Rule (reportingDescriptor)
# ---------------------------------------------------------------------------


class ReportingConfiguration(BaseModel):
    """Default enforcement configuration for a rule."""

    level: str = "warning"
    """Default level: ``"error"`` (block) or ``"warning"`` (warn)."""

    enabled: bool = True

    model_config = _SARIF_COMMON


class ReportingDescriptor(BaseModel):
    """A rule definition in the tool's rule catalog.

    Maps to a ``RuleDefinition`` from ``catalog.yaml`` — the SARIF wire form
    of a rule.
    """

    id: str
    """Stable rule identifier, e.g. ``"LOG001"``."""

    name: str | None = None
    """Short CamelCase name, e.g. ``"NoFStringInLog"``."""

    short_description: dict[str, str] | None = None
    """``{text: "one-liner"}``."""

    full_description: dict[str, str] | None = None
    """``{text/markdown: "extended description"}``."""

    help_uri: str | None = None
    """Link to rule documentation."""

    default_configuration: ReportingConfiguration = Field(
        default_factory=ReportingConfiguration
    )

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/*`` governance fields; see ``AtlanRuleProperties``."""

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ToolComponent(BaseModel):
    """The tool that ran the checks (``tool.driver``)."""

    name: str
    version: str | None = None
    semantic_version: str | None = None
    information_uri: str | None = None
    rules: list[ReportingDescriptor] = []

    model_config = _SARIF_COMMON


class Tool(BaseModel):
    """SARIF ``tool`` object."""

    driver: ToolComponent

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


class Invocation(BaseModel):
    """Records how and when the suite was invoked."""

    execution_successful: bool = True
    """``True`` if the suite ran to completion (regardless of violations found)."""

    exit_code: int | None = None
    """0 = gate passed; 1 = gate failed (≥1 FAILING result)."""

    exit_code_description: str | None = None

    working_directory: ArtifactLocation | None = None

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Version-control provenance
# ---------------------------------------------------------------------------


class VersionControlDetails(BaseModel):
    """Identifies the repo + commit being analysed."""

    repository_uri: str
    revision_id: str | None = None
    """Commit SHA."""

    branch: str | None = None
    mapped_to: ArtifactLocation | None = None

    model_config = _SARIF_COMMON


# ---------------------------------------------------------------------------
# Run + top-level report
# ---------------------------------------------------------------------------


class SarifRun(BaseModel):
    """A single suite execution over one repo.

    ``properties["atlan/summary"]`` carries per-disposition counts for cheap
    dashboarding without parsing every result:
    ``{failing, warning, suppressing}``.
    """

    tool: Tool
    results: list[Result] = []
    invocations: list[Invocation] = []
    version_control_provenance: list[VersionControlDetails] = []
    automation_details: dict[str, Any] | None = None
    """Optional ``{id, description}`` for fleet-level run identification."""

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/profileVersion`` + ``atlan/summary`` totals."""

    model_config = _SARIF_COMMON


class SarifReport(BaseModel):
    """Top-level SARIF 2.1.0 log file.

    Serialise with ``model.model_dump(by_alias=True, exclude_none=True)``
    to get spec-compliant JSON.
    """

    version: str = "2.1.0"
    runs: list[SarifRun] = []

    # version/runs need no camelCase aliases; $schema is injected by the serializer.
    model_config = ConfigDict(populate_by_name=True)

    @model_serializer(mode="wrap")
    def _inject_schema(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["$schema"] = _SARIF_SCHEMA_URL
        return data

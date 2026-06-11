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

SARIF reference: https://docs.oasis-open.org/sarif/sarif/v2.1.0/
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


class Region(BaseModel):
    """A region within an artifact (file)."""

    start_line: int = Field(..., alias="startLine", ge=1)
    start_column: int = Field(1, alias="startColumn", ge=1)
    end_line: int | None = Field(None, alias="endLine")
    end_column: int | None = Field(None, alias="endColumn")
    snippet: dict[str, str] | None = None
    """Optional ``{text: "..."}`` snippet for context."""

    model_config = {"populate_by_name": True}


class ArtifactLocation(BaseModel):
    """Reference to a file artifact."""

    uri: str
    """Relative or absolute URI.  Use repo-root-relative paths for portability."""

    uri_base_id: str | None = Field(None, alias="uriBaseId")
    """Symbolic root, e.g. ``%SRCROOT%``."""

    model_config = {"populate_by_name": True}


class PhysicalLocation(BaseModel):
    """A physical location within a source file."""

    artifact_location: ArtifactLocation = Field(..., alias="artifactLocation")
    region: Region | None = None

    model_config = {"populate_by_name": True}


class Location(BaseModel):
    """A location associated with a result."""

    physical_location: PhysicalLocation | None = Field(None, alias="physicalLocation")
    message: dict[str, str] | None = None
    """Optional ``{text: "..."}`` message attached to the location."""

    model_config = {"populate_by_name": True}


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

    deleted_region: Region = Field(..., alias="deletedRegion")
    inserted_content: dict[str, str] | None = Field(None, alias="insertedContent")
    """``{text: "replacement text"}``."""

    model_config = {"populate_by_name": True}


class ArtifactChange(BaseModel):
    """Proposed changes to a single artifact."""

    artifact_location: ArtifactLocation = Field(..., alias="artifactLocation")
    replacements: list[Replacement] = []

    model_config = {"populate_by_name": True}


class Fix(BaseModel):
    """A proposed fix for a result."""

    description: dict[str, str] | None = None
    """``{text: "..."}`` description of what the fix does."""

    artifact_changes: list[ArtifactChange] = Field([], alias="artifactChanges")

    model_config = {"populate_by_name": True}


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

    rule_id: str = Field(..., alias="ruleId")
    """Must match a ``reportingDescriptor.id`` in ``tool.driver.rules``."""

    rule_index: int | None = Field(None, alias="ruleIndex")
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

    partial_fingerprints: dict[str, str] = Field(
        default_factory=dict, alias="partialFingerprints"
    )
    """Stable identity hashes for deduplication and oscillation detection.
    Key ``"atlanConformance/v1"`` = ``hash(ruleId + normalised_uri + startLine)``."""

    suppressions: list[Suppression] = []
    """Non-empty ↔ this result is intentionally suppressed."""

    fixes: list[Fix] = []
    """Proposed machine-actionable edits (populated by the remediation layer)."""

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/*`` extension properties; see ``AtlanResultProperties``."""

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Rule (reportingDescriptor)
# ---------------------------------------------------------------------------


class ReportingConfiguration(BaseModel):
    """Default enforcement configuration for a rule."""

    level: str = "warning"
    """Default level: ``"error"`` (block) or ``"warning"`` (warn)."""

    enabled: bool = True

    model_config = {"populate_by_name": True}


class ReportingDescriptor(BaseModel):
    """A rule definition in the tool's rule catalog.

    Maps to a ``RuleDefinition`` from ``catalog.yaml`` — the SARIF wire form
    of a rule.
    """

    id: str
    """Stable rule identifier, e.g. ``"LOG001"``."""

    name: str | None = None
    """Short CamelCase name, e.g. ``"NoFStringInLog"``."""

    short_description: dict[str, str] | None = Field(None, alias="shortDescription")
    """``{text: "one-liner"}``."""

    full_description: dict[str, str] | None = Field(None, alias="fullDescription")
    """``{text/markdown: "extended description"}``."""

    help_uri: str | None = Field(None, alias="helpUri")
    """Link to rule documentation."""

    default_configuration: ReportingConfiguration = Field(
        default_factory=ReportingConfiguration,
        alias="defaultConfiguration",
    )

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/*`` governance fields; see ``AtlanRuleProperties``."""

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ToolComponent(BaseModel):
    """The tool that ran the checks (``tool.driver``)."""

    name: str
    version: str | None = None
    semantic_version: str | None = Field(None, alias="semanticVersion")
    information_uri: str | None = Field(None, alias="informationUri")
    rules: list[ReportingDescriptor] = []

    model_config = {"populate_by_name": True}


class Tool(BaseModel):
    """SARIF ``tool`` object."""

    driver: ToolComponent

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


class Invocation(BaseModel):
    """Records how and when the suite was invoked."""

    execution_successful: bool = Field(True, alias="executionSuccessful")
    """``True`` if the suite ran to completion (regardless of violations found)."""

    exit_code: int | None = Field(None, alias="exitCode")
    """0 = gate passed; 1 = gate failed (≥1 FAILING result)."""

    exit_code_description: str | None = Field(None, alias="exitCodeDescription")

    working_directory: ArtifactLocation | None = Field(None, alias="workingDirectory")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Version-control provenance
# ---------------------------------------------------------------------------


class VersionControlDetails(BaseModel):
    """Identifies the repo + commit being analysed."""

    repository_uri: str = Field(..., alias="repositoryUri")
    revision_id: str | None = Field(None, alias="revisionId")
    """Commit SHA."""

    branch: str | None = None
    mapped_to: ArtifactLocation | None = Field(None, alias="mappedTo")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Run + top-level report
# ---------------------------------------------------------------------------


class SarifRun(BaseModel):
    """A single suite execution over one repo.

    ``properties["atlan/summary"]`` carries per-disposition counts for cheap
    dashboarding without parsing every result:
    ``{passing, failing, warning, suppressed}``.
    """

    tool: Tool
    results: list[Result] = []
    invocations: list[Invocation] = []
    version_control_provenance: list[VersionControlDetails] = Field(
        [], alias="versionControlProvenance"
    )
    automation_details: dict[str, Any] | None = Field(None, alias="automationDetails")
    """Optional ``{id, description}`` for fleet-level run identification."""

    properties: dict[str, Any] = Field(default_factory=dict)
    """``atlan/profileVersion`` + ``atlan/summary`` totals."""

    model_config = {"populate_by_name": True}


class SarifReport(BaseModel):
    """Top-level SARIF 2.1.0 log file.

    Serialise with ``model.model_dump(by_alias=True, exclude_none=True)``
    to get spec-compliant JSON.
    """

    version: str = "2.1.0"
    schema_: str = Field(
        "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        alias="$schema",
    )
    runs: list[SarifRun] = []

    model_config = {"populate_by_name": True}

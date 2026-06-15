"""Typed models for the ``atlan/*`` SARIF ``properties`` extensions.

These make the ``properties`` bags first-class typed objects so rule-checker
code never has to write raw string keys.  The models are *not* part of the
SARIF wire format — they are serialised *into* the ``properties`` dict before
emitting a report.

Usage::

    rule_props = AtlanRuleProperties(
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
    )
    descriptor.properties.update(rule_props.to_properties())
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from suite.schema.disposition import EnforcementTier, RuleMechanism

# ---------------------------------------------------------------------------
# Rule-level (reportingDescriptor.properties)
# ---------------------------------------------------------------------------


class AtlanRuleProperties(BaseModel):
    """Governance metadata attached to each rule's ``reportingDescriptor``.

    Serialised into the ``atlan/*`` namespace inside ``properties``.
    """

    tier: EnforcementTier
    """``warn`` → counted, non-blocking; ``block`` → gate-failing."""

    mechanism: RuleMechanism
    """``static`` (AST/regex, fast) or ``test`` (execution-required, slow).
    Lets the remediation loop re-run only the narrowest gate covering a fix.
    """

    category: str
    """Rule family, e.g. ``"silent-swallow"``, ``"log-format"``.
    Used for fleet-level dashboarding and suppression-rate signals.
    """

    autofixable: bool = False
    """``True`` if the remediation layer can produce a mechanical fix without
    model judgment."""

    orthogonal_gate: str | None = None
    """The gate a remediation of this rule may *not* modify in the same
    change (§6.1 of the project design doc).  E.g. ``"tests"`` means a
    code-fix PR must not touch test files.
    """

    since: str | None = None
    """Suite (SDK) version when this rule was introduced, e.g. ``"3.16.0"``."""

    def to_properties(self) -> dict[str, Any]:
        """Return a ``properties`` dict ready to merge into a SARIF node."""
        out: dict[str, Any] = {
            "atlan/tier": self.tier.value,
            "atlan/mechanism": self.mechanism.value,
            "atlan/category": self.category,
            "atlan/autofixable": self.autofixable,
        }
        if self.orthogonal_gate is not None:
            out["atlan/orthogonalGate"] = self.orthogonal_gate
        if self.since is not None:
            out["atlan/since"] = self.since
        return out

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> AtlanRuleProperties:
        """Parse from a raw ``properties`` dict (e.g. when reading a catalog entry)."""
        return cls(
            tier=EnforcementTier(props["atlan/tier"]),
            mechanism=RuleMechanism(props["atlan/mechanism"]),
            category=props["atlan/category"],
            autofixable=bool(props.get("atlan/autofixable", False)),
            orthogonal_gate=props.get("atlan/orthogonalGate"),
            since=props.get("atlan/since"),
        )


# ---------------------------------------------------------------------------
# Result-level (result.properties)
# ---------------------------------------------------------------------------


class AtlanResultProperties(BaseModel):
    """Per-finding metadata in ``result.properties``."""

    hint: str | None = None
    """Machine-actionable remediation hint for the model (e.g. the canonical
    fix template ID, or a short description of what to change).
    """

    external_influence: bool = False
    """``True`` if the remediation loop's input included untrusted external
    content (§6.4).  Set by the remediation layer, not by the checker.
    Results with this flag set are routed to human review regardless of gate
    status.
    """

    def to_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.hint is not None:
            out["atlan/hint"] = self.hint
        if self.external_influence:
            out["atlan/externalInfluence"] = True
        return out

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> AtlanResultProperties:
        return cls(
            hint=props.get("atlan/hint"),
            external_influence=bool(props.get("atlan/externalInfluence", False)),
        )


# ---------------------------------------------------------------------------
# Run-level (run.properties)
# ---------------------------------------------------------------------------


class DispositionSummary(BaseModel):
    """Per-disposition counts for cheap dashboarding."""

    failing: int = 0
    warning: int = 0
    suppressing: int = 0

    def to_properties(self) -> dict[str, Any]:
        return {
            "atlan/summary": {
                "failing": self.failing,
                "warning": self.warning,
                "suppressing": self.suppressing,
            }
        }


class AtlanRunProperties(BaseModel):
    """Metadata attached to a ``SarifRun``."""

    profile_version: str = "v1"
    """Atlan SARIF profile version — distinct from SARIF's ``"2.1.0"``."""

    summary: DispositionSummary | None = None
    """Disposition counts; populated by ``ReportBuilder.build()``."""

    excluded_paths: list[str] = []
    """Repo-root-relative path prefixes that were excluded from scanning entirely.

    Populated from the runner's ``--exclude`` option.  Recorded here so every
    SARIF artifact documents the effective scan scope — a non-empty list is an
    intentional reduction of coverage and should be reviewed as such.
    """

    def to_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {"atlan/profileVersion": self.profile_version}
        if self.summary is not None:
            out.update(self.summary.to_properties())
        if self.excluded_paths:
            out["atlan/excludedPaths"] = sorted(self.excluded_paths)
        return out

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> AtlanRunProperties:
        summary_raw = props.get("atlan/summary")
        summary = DispositionSummary(**summary_raw) if summary_raw else None
        return cls(
            profile_version=props.get("atlan/profileVersion", "v1"),
            summary=summary,
            excluded_paths=props.get("atlan/excludedPaths", []),
        )


# Public alias used externally
AtlanProperties = AtlanRuleProperties

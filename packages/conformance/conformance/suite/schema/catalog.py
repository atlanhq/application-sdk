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

import re
from typing import Any, Literal

from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)
from pydantic import BaseModel, Field, model_validator

#: The two accepted spellings of :attr:`RuleDefinition.superseded_by` — a rule ID
#: (another rule takes the surface over) or an ``sdk>=X.Y.Z`` marker (an SDK fix
#: removes the condition).  An exhaustive pattern rather than free text so a typo
#: (``"sdk >= 3.27"``, ``"P42"``) is caught at rule-definition time, the same way
#: ``orthogonal_gate``'s ``Literal`` catches a mistyped gate name.
_SUPERSEDED_BY_RE = re.compile(r"^(?:[A-Z]\d{3}|sdk>=\d+(?:\.\d+)*)$")

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

    fix_locus: FixLocus | None = None
    """Where the fix belongs, *when that is not the surface* ``scope`` *implies*.

    ``scope`` already says which repos the rule runs against, and the fix
    normally lands in that repo's own hand-written source.  So this field is
    exception-only: set it when the edit belongs somewhere a reader would not
    look first — the pkl contract, the toolkit renderer, packaging descriptors,
    ``.github/``, the test suite — and leave it unset otherwise.

    ``None`` reads as *the hand-written source of the repo under scan*, which is
    also the honest answer for a ``both``-scoped rule: a literal ``app`` would be
    wrong every time such a rule fires on the SDK.  Declaring the implied locus
    is rejected outright — see :meth:`_reject_redundant_fix_locus`."""

    canonical_reference: str = ""
    """A file in a maintained reference app that already has the compliant shape.

    Required for every ``app``- and ``both``-scoped rule
    (``test_app_facing_rules_name_a_canonical_reference``), because those are the
    rules an app engineer has to act on, and "what does correct look like here"
    is the question the finding text cannot answer.

    Only the four public reference apps count — ``atlan-hello-world-app``,
    ``atlan-openapi-app``, ``atlan-mysql-app``, ``atlan-metabase-app`` — plus
    ``application_sdk`` itself for rules about SDK-owned surfaces.  An arbitrary
    connector may be mid-migration and is not a model of anything (see
    ``docs/agents/canonical-apps.md``).

    Name a path, not a sentiment: the value must carry a concrete file so a
    reader can open it.  Two rules may not share the same reference — if they
    would, the reference is too coarse to have been read from either."""

    rule_interactions: str = ""
    """Other rules or gates that constrain this one's fix.

    Some fixes are boxed in by a second rule and the obvious remedy is illegal:
    narrowing an ``@entrypoint`` field's type to satisfy one rule trips another,
    and the append-only ledger guard blocks the retype outright.  Stating the
    interaction here stops each reader re-deriving the deadlock."""

    terminal_state: str = ""
    """What "already correct" looks like, when that is not simply zero findings.

    For a suppress-only rule a justified inline directive IS the fix, not a
    failure to remediate.  Without saying so, an automated lane re-opens settled
    work every cycle and a reviewer cannot tell a deliberate carve-out from an
    unfixed violation."""

    autofixable: bool = False
    short_description: str = ""
    full_description: str = ""
    help_uri: str | None = None
    orthogonal_gate: Literal["tests", "pkl-eval", "skip", "docker-build"] | None = None
    """Named gate to run after a source-code fix.  ``None`` or ``"tests"`` runs
    the repository's standard test suite; ``"pkl-eval"`` runs the pkl-eval gate;
    ``"docker-build"`` builds the app's Dockerfile (the I-series gate — a
    container-image edit cannot move the Python test suite, so ``"tests"`` there
    would pass on any edit at all, and ``"skip"``'s parse check has no parser for
    Dockerfile syntax); ``"skip"`` skips gating entirely — for fixes that cannot
    affect Python or contract behaviour (e.g. a deterministic re-sync of a managed
    CI/scaffold file) so running the test suite would only add cost with no signal.
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
    until: str | None = None
    """Conformance suite version at which this rule retires — the first version
    that must no longer ship it.  ``None`` means indefinite enforcement.

    The counterpart to ``since``, and the reason it exists: a rule landed as an
    *interim net* for a defect fixed elsewhere (an SDK fix, a platform change)
    otherwise has no retirement path and becomes permanent by construction.
    Naming the version here is a forcing function, not a comment —
    ``test_catalog_retired_rules_are_removed`` fails once the package version
    reaches ``until`` and the rule is still in the catalog, the same way the
    deprecation drift gate fails on a stale manifest."""
    superseded_by: str | None = None
    """What makes this rule unnecessary, if anything does.  Two forms:

    * a **rule ID** (``"P042"``) — another rule takes the surface over, e.g.
      when one rule is split in two;
    * an **``sdk>=X.Y.Z`` marker** — an SDK fix removes the condition at its
      root, so the rule only describes apps pinned below that version.

    Recording it separately from ``until`` is deliberate: the *trigger* for
    retirement is usually known long before the conformance version that will
    carry it out (an SDK fix ships on the SDK's cadence, and the rule must keep
    firing until the fleet floor crosses it).  A ``superseded_by`` with no
    ``until`` is the normal steady state for such a rule; ``until`` is filled in
    once the retirement version is actually decided."""
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
            if "fix_locus" in data and isinstance(data["fix_locus"], str):
                data["fix_locus"] = FixLocus(data["fix_locus"].lower())
        return data

    @model_validator(mode="after")
    def _reject_redundant_fix_locus(self) -> RuleDefinition:
        """Reject a ``fix_locus`` that only restates ``scope``.

        Half the catalog once carried ``fix_locus=APP`` next to ``scope=app`` or
        ``scope=both``, and every ``scope=sdk`` rule carried ``fix_locus=SDK``.
        Neither told a reader anything ``scope`` had not already told them, and a
        field that is usually noise stops being read on the occasions it matters
        — which is exactly when the fix is in the toolkit or the contract.

        Enforcing it here rather than in a meta-test means the redundant form
        cannot be constructed at all, so it cannot come back one rule at a time.
        """
        if self.fix_locus is None:
            return self
        implied = FixLocus.SDK if self.scope is RuleScope.SDK else FixLocus.APP
        if self.fix_locus is implied:
            raise ValueError(
                f"{self.id}: fix_locus={self.fix_locus.value!r} only restates "
                f"scope={self.scope.value!r} — omit it.  Declare fix_locus only "
                f"when the fix belongs somewhere else (contract, toolkit, "
                f"packaging, ci, tests)."
            )
        return self

    @model_validator(mode="after")
    def _validate_superseded_by(self) -> RuleDefinition:
        """Reject a supersession marker that nothing can act on.

        A free-text ``superseded_by`` would be silently ignored by every reader,
        so a typo (``"sdk >= 3.27"``, ``"P42"``) is rejected at rule-definition
        time.  The ordering invariant between ``since`` and ``until``, and the
        retirement gate itself, live in ``tests/test_catalog.py`` — version
        comparison belongs to the check layer, and the schema layer stays free
        of upward imports.
        """
        if self.superseded_by is None:
            return self
        if not _SUPERSEDED_BY_RE.match(self.superseded_by):
            raise ValueError(
                f"{self.id}: superseded_by must be a rule ID (e.g. 'P042') or an "
                f"'sdk>=X.Y.Z' marker, got {self.superseded_by!r}"
            )
        if self.superseded_by == self.id:
            raise ValueError(f"{self.id}: superseded_by cannot name the rule itself")
        return self

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
            until=self.until,
            superseded_by=self.superseded_by,
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

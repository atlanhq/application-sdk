"""Backwards-compatibility / deprecation rule definitions (B-series).

The B-series governs the *deprecation lifecycle* across the fleet — both sides of
it:

* **Consumer side (B001, scope ``app``)** — an app keeps using an SDK symbol the
  SDK has marked deprecated.  Driven by a committed manifest generated from SDK
  source, so every future deprecation fans out as a fleet-wide signal with no
  per-app work (BLDX-1418).
* **Authoring side (B002/B003/B004, scope ``sdk``)** — the SDK must declare its
  deprecations correctly: each notice names a replacement and a removal version
  (B002), no deprecation outlives its promised removal version (B003), and a
  docstring deprecation claim is backed by a real marker (B004).

Rule-id stability: B-ids are a permanent public contract (exposed in SARIF
``help_uri`` and referenced by inline ``# conformance: ignore[Bxxx]``
suppressions).  An id never migrates, changes, or gets reused.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HELP_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
    "conformance/docs/rules/deprecation.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="B001",
        scope=RuleScope.APP,
        name="DeprecatedSdkSymbolUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="deprecated-symbol-usage",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "A symbol the SDK has marked deprecated is on a removal path: code that "
            "imports, subclasses, or calls it will break at the next major SDK bump. "
            "Surfacing that consumption now — while the deprecation notice still "
            "carries the SDK's own migration guidance — turns every future SDK "
            "deprecation into a fleet-wide nudge with zero per-app configuration "
            "(BLDX-1418). The deprecated set is a committed manifest generated from "
            "SDK source, so apps need neither the SDK source nor any local list. WARN "
            "because the migration changes call shapes (signatures, return types) and "
            "needs human judgement, not a blind swap."
        ),
        short_description=(
            "Imports, subclasses, or calls an SDK symbol the SDK has marked deprecated"
        ),
        full_description=(
            "Flags app consumption of any symbol recorded in the deprecated-symbol\n"
            "manifest the SDK ships with this conformance package (BLDX-1418).  Three\n"
            "surfaces are matched, name-anchored within an ``application_sdk`` import\n"
            "context:\n"
            "\n"
            "* importing a deprecated class/function "
            "(``from application_sdk.x import Foo``);\n"
            "* subclassing a deprecated base "
            "(``class MyExtractor(BaseMetadataExtractor)``);\n"
            "* calling a deprecated method by attribute (``obj.upload_to_atlan(...)``).\n"
            "\n"
            "The finding carries the SDK's migration guidance from the deprecation\n"
            "notice so the fix is concrete.  Complements E013 ``LegacyAtlanErrorRaise``\n"
            "(which owns the ``raise AtlanError`` site); B001 owns import / construct /\n"
            "subclass, so the two never double-report.\n"
            "\n"
            "Coverage limits (intentional, low-false-positive at WARN): deprecated\n"
            "*parameters* / *modes* are out of scope; method matching is\n"
            "attribute-name-anchored (a same-named method on an unrelated object is a\n"
            "known false-positive risk); re-export aliasing can produce false\n"
            "negatives.  All are suppressible with ``# conformance: ignore[B001]``.\n"
        ),
        help_uri=f"{_HELP_BASE}#b001",
    ),
    RuleDefinition(
        id="B002",
        scope=RuleScope.SDK,
        name="MalformedDeprecationNotice",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="deprecation-hygiene",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "A deprecation notice exists to migrate callers off a symbol before it is "
            "removed. To do that it must answer two questions: what to use instead, "
            "and by when the symbol disappears. A notice missing either is a dead end "
            "— callers see 'deprecated' with no path forward, and B001 cannot carry a "
            "migration hint to the fleet. Enforcing 'names a replacement AND a removal "
            "version' on the SDK's own notices keeps the whole deprecation pipeline "
            "actionable. WARN: the required wording is a guided edit, not mechanical."
        ),
        short_description=(
            "A deprecation notice must name both a migration target and a removal "
            "version"
        ),
        full_description=(
            "Flags a ``@deprecated(...)`` or ``DeprecationWarning`` notice in SDK\n"
            "source whose message does not name *both*:\n"
            "\n"
            "* a **migration target** — what to use instead "
            "(``use X`` / ``Use X instead`` / ``Migrate to Y``); and\n"
            "* a **removal version** — when it goes away "
            "(``will be removed in v4.0``).\n"
            "\n"
            "Both are required so every deprecation is self-describing and B001 can\n"
            "propagate concrete guidance to consumers.  Only symbol-level markers are\n"
            "checked (decorator, or a class emitting ``DeprecationWarning`` from\n"
            "``__init__`` / ``__init_subclass__``).\n"
        ),
        help_uri=f"{_HELP_BASE}#b002",
    ),
    RuleDefinition(
        id="B003",
        scope=RuleScope.SDK,
        name="OverdueDeprecationRemoval",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="deprecation-hygiene",
        autofixable=False,
        since="0.5.0",
        rationale=(
            "A deprecation that promises 'removed in v4.0' is a commitment. If the SDK "
            "is already at or past that version and the symbol is still present, the "
            "deprecation has silently become permanent: dead-weight code, a broken "
            "promise to callers, and a removal that will now surprise people whenever "
            "it finally lands. Comparing each notice's stated removal version against "
            "the SDK's own [project].version makes overdue removals visible the moment "
            "they expire. Detect-only: removing a public symbol (or re-scheduling it) "
            "is a human decision routed to residue, never an automated edit."
        ),
        short_description=(
            "A deprecation's stated removal version has already been reached by the SDK"
        ),
        full_description=(
            "Flags a marked deprecation whose ``removed in vX`` version is <= the\n"
            "SDK's current ``[project].version``.  The symbol was promised gone by now\n"
            "but is still present.  The fix — removing the public symbol or pushing the\n"
            "removal version out — is a human call, so this is detect-only and routes\n"
            "to residue in the remediation loop.  When the SDK version cannot be\n"
            "determined the check is skipped (overdue-ness is undecidable).\n"
        ),
        help_uri=f"{_HELP_BASE}#b003",
    ),
    RuleDefinition(
        id="B004",
        scope=RuleScope.SDK,
        name="UnmarkedDeprecationClaim",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="deprecation-hygiene",
        autofixable=False,
        since="0.5.0",
        rationale=(
            "A docstring that says 'Deprecated: use X instead' communicates intent to "
            "a human reader but enforces nothing: no runtime DeprecationWarning, no "
            "@deprecated, and — critically — nothing the manifest can pick up, so B001 "
            "never warns the fleet. The claim and the mechanism have drifted apart. "
            "Flagging a deprecation claim that lacks a real marker closes that gap so "
            "every stated deprecation is also a detectable one. Detect-only: which "
            "marker to add (decorator vs warning) is a small design choice for the "
            "author, routed to residue with a concrete suggestion."
        ),
        short_description=(
            "A symbol's docstring claims deprecation but it carries no @deprecated / "
            "DeprecationWarning marker"
        ),
        full_description=(
            "Flags a function / method / class whose docstring opens with\n"
            "``Deprecated`` (or carries a ``.. deprecated::`` directive) but has no\n"
            "machine-readable marker — no ``@deprecated`` decorator and no\n"
            "``DeprecationWarning`` emitted from ``__init__`` / ``__init_subclass__``.\n"
            "Such a claim is invisible to consumers and to B001.  Detect-only: the\n"
            "remediation loop routes it to residue with a suggestion to add a marker.\n"
            "\n"
            "Scoped to real symbol docstrings (not, e.g., a field literally named\n"
            "``deprecated``), biased toward low false positives at WARN.\n"
        ),
        help_uri=f"{_HELP_BASE}#b004",
    ),
)

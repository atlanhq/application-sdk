"""Dependency conformance rule definitions (D-series).

Apps depend on ``atlan-application-sdk`` for runtime contracts and managed
transitive dependencies.  Drift between an app's ``pyproject.toml`` and the
SDK's pinned dependencies is the dominant source of breakage during fleet-wide
SDK upgrades.  These rules enforce two invariants:

* ``D001`` — the SDK is declared with a bounded version specifier so every
  upgrade is intentional and reviewed.
* ``D002`` — packages already pinned by the SDK are not redeclared in the
  app's own ``[project.dependencies]`` (or per-extra arrays), where they would
  silently override the SDK's pin.
* ``D003`` — packages declared in the repo's core ``[project.dependencies]``
  are actually imported somewhere in source; a declared-but-never-imported
  dependency is flagged for review (it may be dead weight, or it may be loaded
  dynamically / via an entry point / as a server — hence advisory, not a block).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="D001",
        scope=RuleScope.APP,
        name="UnpinnedSdkDependency",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "An unbounded specifier lets an automated tool (Renovate) or a manual bump "
            "pull in a future SDK major without review. The SDK's versioning discipline only "
            "holds if every app has a bound that stops automatic upgrades past the reviewed "
            "point."
        ),
        short_description=(
            "Application SDK dependency is missing or its version specifier is "
            "not bounded on both ends"
        ),
        full_description=(
            "Every app must declare ``atlan-application-sdk`` in "
            "``[project.dependencies]`` with a version specifier that has both "
            "a lower bound (``>=`` or ``==``) and an upper bound (``<`` or a "
            "compatible-release ``~=`` form). Unbounded specifiers let an "
            "automated SDK upgrade pull in a future major version without "
            "review, defeating the fleet-wide gate.  Apps shipping the SDK "
            "are also exempt — packages whose ``[project].name`` starts with "
            "``atlan-application-sdk`` are skipped entirely."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d001"
        ),
    ),
    RuleDefinition(
        id="D002",
        scope=RuleScope.APP,
        name="RedeclaredSdkManagedDependency",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "When an app redeclares a package the SDK already pins, the resolver may pick "
            "the app's specifier over the SDK's, yielding a version never validated against "
            "the SDK. This causes resolver conflicts during upgrades and forces touching "
            "every app that holds a duplicate when the SDK pin changes."
        ),
        short_description=(
            "Dependency redeclared in the app's pyproject.toml is already "
            "managed by the SDK"
        ),
        full_description=(
            "Packages pinned by ``atlan-application-sdk`` (its core "
            "``[project.dependencies]``) must not be redeclared in the app's "
            "``[project.dependencies]`` or any ``[project.optional-"
            "dependencies.*]`` array.  Redeclaring a managed pin lets the app "
            "silently override the SDK's contract, causing resolver conflicts "
            "and drift across the fleet during automated SDK upgrades.  The "
            "SDK's managed set is read at check time via "
            "``importlib.metadata.requires('atlan-application-sdk')``; if the "
            "SDK is not importable in the runtime environment, this rule is "
            "skipped silently."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d002"
        ),
    ),
    RuleDefinition(
        id="D003",
        scope=RuleScope.BOTH,
        name="UnusedDependency",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-hygiene",
        autofixable=False,
        since="0.5.0",
        rationale=(
            "A package declared in core dependencies but never imported is either dead "
            "weight that slows resolution and widens the supply-chain/CVE surface, or it "
            "was meant to live elsewhere (a test/dev group). Surfacing it turns the "
            "recurring manual question during a version bump — 'is this even used?' — "
            "into a deterministic, reviewable signal. It stays advisory (WARN, no "
            "autofix) because a dependency can be loaded dynamically, via an entry "
            "point/plugin, or run as a server (e.g. uvicorn) without an explicit import."
        ),
        short_description=(
            "A package declared in [project.dependencies] is never imported in source"
        ),
        full_description=(
            "Every package in the repo's core ``[project.dependencies]`` should be "
            "imported somewhere in the shipped source.  This rule maps each declared "
            "distribution to the import name(s) it provides and flags any whose modules "
            "never appear in an ``import``/``from`` statement across the repo's Python "
            "sources (tests, build, and dot-directories are excluded — a runtime "
            "dependency used *only* under ``tests/`` is itself a finding, because it "
            "belongs in a test group, not core dependencies).  The finding is advisory: "
            "before removing, confirm the dependency is not imported dynamically (via "
            "``importlib``), pulled in by an entry point or plugin, or required by a "
            "framework/server it is never directly imported by.  Only core "
            "``[project.dependencies]`` is analysed — optional-dependency extras and "
            "dependency groups routinely carry tools and plugins that are legitimately "
            "never imported.  A dependency that cannot be resolved in the analysis "
            "environment is skipped (and reported), never flagged.  "
            "**Operating note:** resolution maps a distribution to its import "
            "name(s) via installed package metadata, so the analysed repo's "
            "dependencies must be importable in the running interpreter — run "
            "``uv sync`` first.  In an isolated runner (e.g. ``uvx "
            "atlan-application-sdk-conformance detect --series D``) no dependency "
            "is installed, so every one is skipped to stderr and the rule reports "
            "nothing; that is an unresolved environment, not a clean repo.  The "
            "conformance CI runs the D-series leg in a synced environment for this "
            "reason.  See BLDX-1462."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d003"
        ),
    ),
)

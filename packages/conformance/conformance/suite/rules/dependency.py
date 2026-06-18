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
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="D001",
        name="UnpinnedSdkDependency",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.4.0",
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
        name="RedeclaredSdkManagedDependency",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.4.0",
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
)

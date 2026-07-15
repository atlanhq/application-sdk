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
* ``D004`` — the D002 check, extended to PEP 735 ``[dependency-groups]``.
* ``D005`` — an ``atlan-application-sdk[extra]`` reference names a published
  extra (uv silently drops unknown extras).
* ``D006`` — the app's ``requires-python`` lower bound is not below the SDK's
  minimum supported Python, so the app never claims support the SDK lacks.
* ``D007`` — the app builds with Hatchling.
* ``D008`` — the app's pyright ``typeCheckingMode`` is not weaker than
  ``standard``.
* ``D009`` — no ``[tool.poe.tasks.*]`` entry fetches Dapr component YAMLs
  from GitHub over the network; the installed SDK wheel bundles them.
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
        id="D004",
        scope=RuleScope.APP,
        name="RedeclaredSdkManagedDependencyInGroups",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "D002 only covers [project.dependencies] and the optional-dependencies arrays; "
            "an SDK-managed package re-pinned in a PEP 735 [dependency-groups] table escapes "
            "it. A dev/test group that re-pins a package the SDK already manages drifts from "
            "the SDK's validated dev environment and must be touched on every SDK bump."
        ),
        short_description=(
            "SDK-managed dependency redeclared in a [dependency-groups] table"
        ),
        full_description=(
            "Packages pinned by ``atlan-application-sdk`` must not be "
            "redeclared in the app's PEP 735 ``[dependency-groups.*]`` tables "
            "(dev/test groups).  This is the coverage gap left by D002, which "
            "scans only ``[project.dependencies]`` and "
            "``[project.optional-dependencies.*]``.  Pull SDK-managed dev/test "
            "tooling in via ``atlan-application-sdk[tests]`` rather than "
            "re-pinning it.  The managed set is read via "
            "``importlib.metadata.requires('atlan-application-sdk')``; if the "
            "SDK is not importable, this rule is skipped silently. "
            "Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d004"
        ),
    ),
    RuleDefinition(
        id="D005",
        scope=RuleScope.APP,
        name="UnknownSdkExtra",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=False,
        since="0.5.0",
        rationale=(
            "uv silently drops an unknown extra, so a typo like "
            "``atlan-application-sdk[dapr]`` (no such extra) installs nothing for that extra "
            "and the missing dependencies surface only at runtime. Validating the reference "
            "against the SDK's published extras catches the silent-failure at build time."
        ),
        short_description=(
            "Reference to an atlan-application-sdk extra the SDK does not publish"
        ),
        full_description=(
            "Every ``atlan-application-sdk[extra]`` reference must name an "
            "extra the SDK actually publishes (its ``Provides-Extra`` "
            "metadata).  An unknown extra is silently dropped by uv, so its "
            "dependencies are never installed and the failure appears only at "
            "runtime.  The published set is read from installed metadata; if "
            "the SDK is not importable, this rule is skipped silently.  The fix "
            "(map a typo to the intended extra) is judgment, so findings route "
            "to residue rather than auto-fix.  Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d005"
        ),
    ),
    RuleDefinition(
        id="D006",
        scope=RuleScope.APP,
        name="IncompatibleRequiresPython",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="python-version",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "An app whose requires-python lower bound is below the SDK's claims to support "
            "an interpreter the SDK does not. Installs on that Python resolve a degraded or "
            "broken dependency set, and the mismatch surfaces only at runtime on the oldest "
            "supported environment — exactly where it is hardest to catch in review."
        ),
        short_description=(
            "App requires-python lower bound is below the SDK's minimum "
            "supported Python version"
        ),
        full_description=(
            "The app's ``[project].requires-python`` lower bound must be at "
            "least the SDK's minimum supported Python (``>=3.11``). A lower "
            "floor lets the app be installed on a Python the SDK never "
            "validated against, where transitive resolution and runtime "
            "behaviour are unsupported. Apps that omit ``requires-python`` or "
            "set a bound at or above the SDK's floor are unaffected. The SDK's "
            "floor is a drift-guarded constant in the checker, not read from "
            "installed metadata, so this rule needs no resolved environment. "
            "Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d006"
        ),
    ),
    RuleDefinition(
        id="D007",
        scope=RuleScope.APP,
        name="NonStandardBuildBackend",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="build-system",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "Atlan apps standardise on Hatchling so build behaviour, wheel layout, and the "
            "managed CI build steps are uniform across the fleet. A setuptools/poetry-core "
            "backend diverges from that baseline and from the bootstrapped build-and-publish "
            "workflow, making fleet-wide build changes per-app instead of uniform."
        ),
        short_description="Build backend is not Hatchling",
        full_description=(
            "``[build-system].build-backend`` must be ``hatchling.build``.  "
            "Atlan's app fleet standardises on Hatchling so the managed "
            "build-and-publish workflow and wheel layout are uniform; a "
            "different backend diverges from that baseline.  A pyproject with "
            "no ``build-backend`` key is not flagged.  Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d007"
        ),
    ),
    RuleDefinition(
        id="D008",
        scope=RuleScope.APP,
        name="WeakenedTypeChecking",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="tooling-baseline",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "The SDK's typed contracts only protect an app whose type checker actually runs "
            "at the SDK's level. A typeCheckingMode of 'off' or 'basic' lets type regressions "
            "against SDK APIs pass app CI unnoticed, defeating the point of the typed surface."
        ),
        short_description=(
            "pyright typeCheckingMode is weaker than the SDK baseline 'standard'"
        ),
        full_description=(
            "``[tool.pyright].typeCheckingMode`` must not be weaker than the "
            "SDK baseline ``standard`` — ``off`` and ``basic`` are flagged; "
            "``standard`` and ``strict`` pass.  A weakened mode lets type "
            "regressions against the SDK's typed APIs slip through app CI.  A "
            "pyproject that does not set ``typeCheckingMode`` is not flagged; "
            "blanket ``reportX = false`` overrides are out of scope (they can "
            "be legitimate).  Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d008"
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
    RuleDefinition(
        id="D009",
        scope=RuleScope.APP,
        name="RemoteDaprComponentFetch",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dapr-components",
        autofixable=True,
        since="0.12.0",
        rationale=(
            "Fetching Dapr component YAMLs from raw.githubusercontent.com or "
            "the GitHub contents API at build time hits GitHub's unauthenticated "
            "rate limit under CI concurrency, turning routine builds into flaky "
            "429s across the fleet. The hardcoded SDK ref these fetches pin to "
            "also drifts from whatever application-sdk version is actually "
            "locked in the app's own uv.lock. The installed SDK wheel already "
            "bundles these files at application_sdk/components/, so the "
            "network round-trip is both fragile and redundant."
        ),
        short_description=(
            "A poe task fetches Dapr component YAMLs from GitHub instead of "
            "the installed application-sdk wheel"
        ),
        full_description=(
            "No ``[tool.poe.tasks.*]`` entry (in either the shorthand "
            '``task.shell = "..."`` form or the full ``[tool.poe.tasks.'
            "task]`` table form) may reference ``raw.githubusercontent.com`` "
            "or ``api.github.com`` for ``atlanhq/application-sdk``. Dapr "
            "component YAMLs are bundled inside the ``atlan-application-sdk`` "
            "wheel at ``application_sdk/components/`` — copy them from there "
            "instead, e.g. ``shutil.copytree(pathlib.Path(application_sdk."
            "__file__).parent / 'components', 'components', "
            "dirs_exist_ok=True)``. This requires application-sdk to already "
            "be installed into the venv before the task runs (true both "
            "locally and in the Docker build, where ``uv sync`` precedes "
            "``poe download-components``). Inline suppression: "
            "``# conformance: ignore[D009] <reason>`` on the line above the "
            "offending entry."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d009"
        ),
    ),
)

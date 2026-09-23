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
* ``D010`` — an app whose code path uses the SDK query transformer
  (``application_sdk.transformers.query`` / ``transform_metadata``) must
  resolve ``duckdb`` — via the SDK's ``[sql]``/``[incremental]`` extra or a
  direct dependency.  On SDK >= 3.22 (daft extra emptied) a missing duckdb is
  a guaranteed runtime ``ImportError`` in every transform.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="D001",
        canonical_reference=(
            "atlan-openapi-app pyproject.toml — `atlan-application-sdk>=3.24.1,<4.0.0`. "
            "Bounded at both ends: a floor for the features the app uses, a ceiling at the "
            "next major so a breaking release cannot arrive through a lockfile refresh."
        ),
        fix_locus=FixLocus.PACKAGING,
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
            "point. "
            "Customer impact: an unreviewed SDK major rides an automated lockfile bump into "
            "the next release, and its breaking changes surface as connector failures in "
            "customer tenants with no app-code diff that explains them — the hardest kind "
            "of regression to attribute during an incident."
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
        canonical_reference=(
            "atlan-openapi-app pyproject.toml — [project.dependencies] holds exactly "
            "one entry, the SDK. orjson and the pyatlan models are imported under app/ "
            "without being redeclared there or in any dependency group, so there is "
            "one place a version can move."
        ),
        fix_locus=FixLocus.PACKAGING,
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
        canonical_reference=(
            "atlan-metabase-app pyproject.toml — the dev and test groups hold only what "
            "the SDK does not ship (pre-commit, pyright, ruff, poethepoet, testcontainers, "
            "httpx, docker), several with a comment on why. Nothing the SDK already pins "
            "is repeated there."
        ),
        fix_locus=FixLocus.PACKAGING,
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
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — "
            "`atlan-application-sdk[iam-auth,sql,workflows,pandas]`. All four are extras "
            "the SDK publishes; a typo here resolves to nothing and fails at import, not "
            "at install."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.APP,
        name="UnknownSdkExtra",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dependency-pinning",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "uv silently drops an unknown extra, so a typo like "
            "``atlan-application-sdk[dapr]`` (no such extra) installs nothing for that extra "
            "and the missing dependencies surface only at runtime. Validating the reference "
            "against the SDK's published extras catches the silent-failure at build time. "
            "Customer impact: the dependencies the app needs are never installed, so the "
            "connector raises ImportError on the first real run in the customer's tenant "
            "— a day-one install failure on an image that passed every build gate, "
            "because the typo is invisible to the resolver that silently dropped it."
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
            "(map a typo to the intended extra) is judgment, so the fix is "
            "written per site rather than applied mechanically.  Cite: BLDX-1410."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d005"
        ),
    ),
    RuleDefinition(
        id="D006",
        canonical_reference=(
            'atlan-openapi-app pyproject.toml — `requires-python = ">=3.11"`, the SDK\'s '
            "own floor. A lower bound than the SDK's promises an interpreter the "
            "dependency tree cannot actually satisfy."
        ),
        fix_locus=FixLocus.PACKAGING,
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
        canonical_reference=(
            'atlan-openapi-app pyproject.toml — `build-backend = "hatchling.build"`, '
            "which is what the app-runtime base image and the publish pipeline expect."
        ),
        fix_locus=FixLocus.PACKAGING,
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
        canonical_reference=(
            'atlan-openapi-app pyproject.toml — `typeCheckingMode = "standard"` under '
            "[tool.pyright], the SDK baseline. Weakening it locally hides exactly the "
            "boundary errors the typed contracts exist to catch."
        ),
        fix_locus=FixLocus.PACKAGING,
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
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — aiomysql is declared in "
            "[project.dependencies] with no Python import anywhere in the repo, and "
            "carries no suppression. SQLAlchemy loads the driver from the "
            '"mysql+aiomysql" dialect string in app/client.py, which the checker reads '
            "via _collect_dialect_drivers, so the dependency is counted as used. A "
            "dynamically-loaded dependency the checker can see is not a finding at all."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[D003] <reason>` IS the correct "
            "end state for a dependency that is genuinely loaded without a static "
            "import — a driver resolved from a dialect/plugin string, an entry-point "
            "registration, a CLI invoked as a subprocess. The reason must name the "
            "mechanism that loads it — and only where the checker cannot already see "
            "that mechanism itself, as it does for atlan-mysql-app's aiomysql. A "
            "directive that only asserts the dependency is needed is unremediated: if "
            "nothing loads it dynamically, remove the dependency rather than "
            "suppressing the finding."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.BOTH,
        name="UnusedDependency",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dependency-hygiene",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "A package declared in core dependencies but never imported is either dead "
            "weight that slows resolution and widens the supply-chain/CVE surface, or it "
            "was meant to live elsewhere (a test/dev group). Surfacing it turns the "
            "recurring manual question during a version bump — 'is this even used?' — "
            "into a deterministic, reviewable signal. It stays advisory (WARN, no "
            "mechanical fix) because a dependency can be loaded dynamically, via an entry "
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
            "reason.  See BLDX-1462.  "
            "**Constraint floors:** in an app repo, every ``[tool.uv] "
            "constraint-dependencies`` entry is also a D003 finding — a security "
            "floor on a transitive package is the SDK's to set, not the app's, and "
            "an app-local copy goes stale when the SDK's range moves.  Remove it; "
            "a needed CVE fix reaches the app by upgrading the SDK.  The SDK's own "
            "pyproject is exempt."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d003"
        ),
    ),
    RuleDefinition(
        id="D009",
        canonical_reference=(
            "atlan-metabase-app pyproject.toml — [tool.poe.tasks.download-components] "
            "runs `shutil.copytree(pathlib.Path(application_sdk.__file__).parent / "
            '"components", "components", dirs_exist_ok=True)` under `interpreter = '
            '"python"`, with a comment saying components/ is gitignored so each '
            "environment copies the set matching the SDK in uv.lock. No poe task names "
            "raw.githubusercontent.com."
        ),
        fix_locus=FixLocus.PACKAGING,
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
            "network round-trip is both fragile and redundant. "
            "Customer impact: the flaky 429 blocks the build pipeline exactly when a "
            "customer is waiting on a hotfix release, and component YAMLs fetched at a "
            "drifted ref can ship state/queue configuration the locked SDK was never "
            "validated against — misbehaving only once deployed in the tenant."
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
    RuleDefinition(
        id="D010",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — the SDK is installed with the `sql` extra, "
            "which is what resolves duckdb. An app importing the SDK query transformer "
            "without one of [sql]/[incremental], or a direct duckdb pin, imports a module "
            "whose engine is absent."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.APP,
        name="QueryTransformerWithoutDuckdb",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="runtime-dependencies",
        autofixable=False,
        since="0.18.0",
        rationale=(
            "The SDK's query transformer (application_sdk.transformers.query, the "
            "transform_metadata path) executes its transform SQL through "
            "DuckDBConnectionManager, and duckdb ships only in the SDK's [sql] and "
            "[incremental] extras — never in core. An app that imports the query "
            "transformer on a plain atlan-application-sdk pin, with no extra at all, "
            "hits a guaranteed runtime ImportError ('duckdb is required for "
            "DuckDBConnectionManager') in EVERY transform — latent until the first "
            "real pipeline run, because imports alone succeed and mocked unit tests "
            "pass. The population that surfaced this was a different, SDK-owned "
            "shape: apps pinned to the deprecated [daft] extra, which resolved "
            "empty over 3.22–3.27 (observed live on main for a document-store "
            "connector in fleet testing after an automated upgrade crossed the 3.22 "
            "line). That half is fixed at the root — [daft] aliases [sql] again from "
            "3.28.0 — so this rule now covers the no-extras case it always also "
            "covered. Statically checkable: transformer-usage scan + "
            "lockfile/pyproject scan. "
            "Customer impact: every transform in the customer's crawl dies with "
            "ImportError, so no metadata reaches their catalog at all — and because "
            "imports succeed and mocked unit tests pass, the first thing that reveals "
            "it is the customer's own failed run."
        ),
        short_description=(
            "App imports the SDK query transformer but duckdb is not resolved "
            "(no [sql]/[incremental] extra, no direct dependency)"
        ),
        full_description=(
            "An app whose source imports the SDK query transformer\n"
            "(``application_sdk.transformers.query`` — the\n"
            "``transform_metadata`` / ``QueryBasedTransformer`` path) must be able\n"
            "to import ``duckdb`` at runtime: the transformer executes its\n"
            "transform SQL through ``DuckDBConnectionManager``, which raises\n"
            "``ImportError: duckdb is required for DuckDBConnectionManager`` when\n"
            "the package is absent.\n"
            "\n"
            "``duckdb`` is provided by the SDK's ``[sql]`` and ``[incremental]``\n"
            "extras only — never by the core dependency set.  So the shape this\n"
            "rule describes is an app that imports the transformer on a **plain**\n"
            "``atlan-application-sdk`` pin, with no extra at all: the failure is\n"
            "silent until the first real end-to-end transform, because imports\n"
            "succeed and unit tests that mock the transformer pass.\n"
            "\n"
            "**Not the ``[daft]`` case — that one was ours.**  Over SDK 3.22–3.27\n"
            "the deprecated ``[daft]`` extra resolved to nothing, so apps that\n"
            "were following the SDK's own deprecation note were broken by an\n"
            "automated upgrade crossing the 3.22 line.  That is fixed at the\n"
            "root: from 3.28.0 ``[daft]`` aliases ``[sql]`` again, and a version\n"
            "bump alone resolves ``duckdb`` for every such app with no repo-side\n"
            "change.  If this rule fires on an app pinned to ``[daft]``, upgrade\n"
            "the SDK rather than editing the app's extras.\n"
            "\n"
            "Resolution order of the check:\n"
            "\n"
            "* with a parseable ``uv.lock`` present, ``duckdb`` must be reachable\n"
            "  from the app's OWN production dependencies — the lock is walked\n"
            "  from the app's ``[[package]]`` entry along ``dependencies`` and the\n"
            "  ``optional-dependencies`` groups an incoming extra activates.\n"
            "  ``uv.lock`` is a *universal* resolution graph covering dev groups\n"
            "  and every extra, so duckdb merely appearing somewhere in it does\n"
            "  NOT mean a default ``uv sync --no-dev`` installs it;\n"
            "  ``[package.dev-dependencies]`` is deliberately not traversed;\n"
            "* without a usable lock, the app's ``pyproject.toml`` must declare\n"
            "  ``duckdb`` directly or reference ``atlan-application-sdk`` with a\n"
            "  ``sql`` or ``incremental`` extra — in ``[project] dependencies``\n"
            "  specifically.  Dependency groups and optional-dependency arrays do\n"
            "  not count: they are not installed by default.\n"
            "\n"
            "**Remediation:** change the SDK reference to\n"
            "``atlan-application-sdk[sql]`` (or ``[incremental]`` for the\n"
            "incremental analytics stack) in ``[project.dependencies]`` and relock\n"
            "(``uv lock``).  That is the fix.\n"
            "\n"
            "Declaring ``duckdb`` directly is a discouraged fallback, not a\n"
            "co-equal option: it clears the finding, but it duplicates a pin the\n"
            "SDK's extras already manage, so the app now owns a version range it\n"
            "has to keep in step with the SDK's by hand.  Reach for it only where\n"
            "the extra genuinely cannot be used.\n"
            "\n"
            "This is a ``BLOCK``: the finding names a *guaranteed* runtime failure,\n"
            "not a risk of one.  It landed as ``WARN`` under the new-rule tier policy\n"
            'with the note "treat it as an error" — the tier now says that instead of\n'
            "asking the reader to.\n"
            "\n"
            "Note for the ``[daft]``-pinned population above: with a parseable\n"
            "``uv.lock`` the check walks what the app's own extras actually resolve,\n"
            "so once the SDK bump to >= 3.28.0 is locked (where ``[daft]`` aliases\n"
            "``[sql]``) ``duckdb`` is reachable and the finding clears with no\n"
            "app-side edit.  Bump the SDK; do not reach for a suppression.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d010"
        ),
    ),
    RuleDefinition(
        id="D011",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — "
            "`atlan-application-sdk-conformance>=0.17.0,<1.0.0` in a dependency group, "
            "with a comment recording that the D-series CI leg resolves the suite from "
            "this repo's own environment. A hard pin freezes that one leg while every "
            "other leg runs the latest; a declaration in [project.dependencies] ships the "
            "linter to production."
        ),
        terminal_state=(
            "The specifier must be able to float. Pinning is what freezes one repo's "
            "D-series leg to a single suite version while every other leg runs the "
            "latest."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.APP,
        name="ConformanceDependencyContract",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dependency-tooling",
        autofixable=True,
        since="0.23.0",
        rationale=(
            "The published remediation programs and the bootstrapped per-app "
            "'remediate' skill invoke the suite as "
            "'uv run atlan-application-sdk-conformance'. In a repo that does not "
            "declare the package that is a hard failure — 'error: Failed to spawn', "
            "exit 2 — because no transitive dependency exposes the console script, "
            "not even a full SDK sync (211 packages, 31 scripts, none of them this "
            "one). So the whole remediation loop is unavailable in a non-declaring "
            "repo: CI can report findings that nobody can then remediate locally or "
            "in-PR. A dev-group declaration restores the loop and never reaches the "
            "runtime image, because dev groups are not installed by a production "
            "sync (FND-419). "
            "The declaration alone is not enough, which is why this rule also "
            "grades its shape: the D-series leg resolves the suite out of the "
            "app's uv.lock, so an exact pin ('==0.13.0') or a '~=' "
            "compatible-release pin freezes the ruleset grading the repo, a "
            "bare '>=0.17.0' leaves it unbounded, and a pyproject-only edit "
            "that never reaches uv.lock leaves the console script absent in "
            "CI, which installs from the lock. A cap at the 1.0.0 major "
            "boundary, locked, is what keeps the repo on a current ruleset "
            "without admitting an unreviewed major. A floor is not required — "
            "it neither freezes nor unfreezes the ruleset, because resolution "
            "takes the newest version under the cap either way — so a plain "
            "'<=1.0.0' and a two-sided '>=0.17.0,<1.0.0' are both accepted. "
            "Customer impact: a repo whose ruleset is frozen silently stops "
            "being graded by every rule added since that version — including "
            "the ones that describe guaranteed runtime failures, such as a "
            "transform path that raises ImportError on every record (D010) or "
            "a Dapr component fetch that rate-limits under CI concurrency "
            "(D009). The app then ships a defect that a current ruleset would "
            "have blocked, and because the gate reported success the first "
            "thing that reveals it is the customer's own failed crawl. Where a "
            "declaration is missing outright, the remediation loop cannot run "
            "in the repo at all, so nothing can be fixed there even once it is "
            "found."
        ),
        short_description=(
            "atlan-application-sdk-conformance is undeclared, declared in "
            "[project.dependencies], pinned to a non-floating specifier, or "
            "missing from uv.lock"
        ),
        full_description=(
            "Every app should declare ``atlan-application-sdk-conformance`` in a\n"
            "dev/test dependency array, with a specifier that can float, and\n"
            "have it resolved in ``uv.lock``.  At most one finding is reported\n"
            "per repo; four things are checked, in order:\n"
            "\n"
            "1. **Declared at all** — satisfied by an entry in **any**\n"
            "   ``[dependency-groups.*]`` group or any\n"
            "   ``[project.optional-dependencies.*]`` array, because apps\n"
            "   legitimately differ on which group they use.\n"
            "2. **Not in** ``[project.dependencies]`` — graded separately,\n"
            "   because a floating runtime entry satisfies every other check\n"
            "   (the console script really does spawn) while shipping a\n"
            "   dev-only tool in the runtime image.  Reported even when a\n"
            "   correct dev-group entry also exists: the runtime line still\n"
            "   has to be deleted.  The fix is to move the entry, not to\n"
            "   add a second one.\n"
            "3. **Specifier can float** — an upper bound, and not a pin.\n"
            "   ``==0.13.0``, ``===0.13.0``, ``~=0.17.0`` and a bare\n"
            "   ``>=0.17.0`` are all rejected.  A floor is optional: it does\n"
            "   not change which version resolves under the cap, so both\n"
            "   ``<=1.0.0`` and ``>=0.17.0,<1.0.0`` are accepted.\n"
            "4. **Present in** ``uv.lock`` — checked only when a lock exists\n"
            "   and parses, so a missing or malformed lock never manufactures\n"
            "   a finding.\n"
            "\n"
            "The canonical form is ``[dependency-groups].dev``:\n"
            '``"atlan-application-sdk-conformance<=1.0.0"``.  A repo already\n'
            "carrying the earlier two-sided form\n"
            '(``">=0.17.0,<1.0.0"``, which ``atlan-app-template`` ships)\n'
            "conforms as it stands and needs no edit.\n"
            "\n"
            "The package is not needed at runtime and must never be added to\n"
            "``[project.dependencies]`` — dev groups are excluded from a\n"
            "production sync, so the declaration cannot reach the shipped image.\n"
            "Branch 2 above is what enforces that placement.\n"
            "\n"
            "**Known trade-off, tracked in FND-419.**  Declaring the package also\n"
            "pins the ruleset used by one CI leg.  The D-series leg is the only\n"
            "one that runs ``uv run --with atlan-application-sdk-conformance``\n"
            "against the app's synced environment; because that ``--with``\n"
            "requirement is unconstrained, uv satisfies it from the app's\n"
            "``uv.lock`` rather than resolving fresh, while the other series legs\n"
            "run ``uvx`` (latest from PyPI).  A declaring repo therefore grades\n"
            "its dependency rules with whatever version its lockfile holds, and\n"
            "the aggregate report can show two driver versions.  Keeping the\n"
            "lockfile current is what limits that; the structural fix is to stop\n"
            "the D leg reading the lockfile at all, which is FND-419's Step 1 +\n"
            "Step 2 and does not require any app-repo change.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d011"
        ),
    ),
    RuleDefinition(
        id="D012",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — `[[tool.uv.index]]` names pypi at "
            "https://pypi.org/simple with `default = true`. Declared in "
            "pyproject.toml rather than a project-level uv.toml, so the repo's "
            "[tool.uv] constraint-dependencies keep being read, and a machine-wide "
            "index cannot rewrite uv.lock on whoever resolves next."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.BOTH,
        name="UnpinnedPackageIndex",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="supply-chain",
        autofixable=True,
        since="0.30.0",
        rationale=(
            "Atlan laptops carry a machine-wide uv default index — the Endor Labs "
            "package firewall, installed into ~/.config/uv/uv.toml with 'default = true' "
            "and credentials inline. A repo that does not pin its own default index "
            "inherits it, and then every 'uv run', 'uv sync' and 'uv lock' rewrites every "
            "package URL in uv.lock to the proxy host. The rewrite is silent in three ways "
            "at once: it prints nothing, it changes no version and no hash, and it touches "
            "a file the task at hand never mentioned — so it is noticed only by someone "
            "who happens to read the diff. A project-level index takes precedence over the "
            "machine-wide one and every uv subcommand reads it, so one stanza closes the "
            "whole class. "
            "Customer impact: the rewritten lock is committed and CI, which holds no "
            "credential for the proxy, fails at dependency install with 401 Unauthorized "
            "on a different package each run. The connector's release stalls behind a "
            "failure that reads as an outage in someone else's infrastructure, and the "
            "cause is invisible in the diff that produced it — no version moved, no "
            "hash moved, only the URLs."
        ),
        short_description=(
            "pyproject.toml does not pin PyPI as the default uv index, so a "
            "machine-wide index can rewrite uv.lock"
        ),
        full_description=(
            "The repo's root ``pyproject.toml`` must declare PyPI as the\n"
            "resolver's default index::\n"
            "\n"
            "    [[tool.uv.index]]\n"
            '    name = "pypi"\n'
            '    url = "https://pypi.org/simple"\n'
            "    default = true\n"
            "\n"
            "Two branches, reported at most once per repo:\n"
            "\n"
            "1. **No default index** — no ``[[tool.uv.index]]`` entry is\n"
            "   marked ``default = true``, so whatever default the machine\n"
            "   supplies is what resolves.\n"
            "2. **Default index is not PyPI** — an entry is marked default\n"
            "   but its URL names some other host, which is the proxy rewrite\n"
            "   already committed into the pyproject.\n"
            "\n"
            "Anchored on the root ``pyproject.toml`` only: this is a property\n"
            "of the repo's resolution, not of each sub-package, so a monorepo\n"
            "gets one finding rather than one per member.\n"
            "\n"
            "The pin belongs in ``pyproject.toml``, **not** in a project-level\n"
            "``uv.toml``.  A ``uv.toml`` does override the user-level file, but\n"
            "it also suppresses ``[tool.uv]`` in ``pyproject.toml`` entirely —\n"
            "silently dropping any ``constraint-dependencies`` CVE floors the\n"
            "repo declares there.  uv warns about that, but names only\n"
            "``constraint-dependencies``.\n"
            "\n"
            "Scope is ``both``: the SDK inherits the machine-wide index exactly\n"
            "as an app does.  This rule pins the index; ``D013`` checks whether\n"
            "a non-PyPI host has already reached ``uv.lock``.  Cite: FND-1928."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d012"
        ),
    ),
    RuleDefinition(
        id="D013",
        canonical_reference=(
            "atlan-openapi-app uv.lock — every download URL names "
            "files.pythonhosted.org or pypi.org and none carries userinfo, so CI "
            "installs from the same host the lock was resolved against. A lock that "
            "has already picked up a proxy host is repaired by restoring the committed "
            "one, not by re-locking on the machine that rewrote it."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.BOTH,
        name="NonPyPILockfileIndex",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="supply-chain",
        autofixable=True,
        since="0.30.0",
        rationale=(
            "D012 is preventive; this is the damage. A uv.lock whose URLs name an internal "
            "proxy pins the repo's resolution to a credentialed host that CI and every "
            "contributor outside the firewall cannot reach, so dependency install fails "
            "with 401 Unauthorized — on a different package each run, because "
            "resolution order varies, which is what makes it read as someone else's outage "
            "rather than as a committed file in this repository. Two Atlan repos have "
            "already lost a debugging cycle to exactly this. The separate credential branch "
            "exists because the two findings are not the same severity: a proxy host in the "
            "lock is a broken build, an index credential in the lock is a secret in version "
            "control that has to be rotated before anything else is done. "
            "Customer impact: the connector cannot be built or released at all until the "
            "lock is repaired, and the pre-release gates that would have caught real "
            "defects never run, because they fail before reaching the code."
        ),
        short_description=(
            "uv.lock resolves packages from a host that is not PyPI, or embeds "
            "an index credential"
        ),
        full_description=(
            "Every download URL in the repo's ``uv.lock`` must name a PyPI\n"
            "host — ``files.pythonhosted.org`` or ``pypi.org``.  Two\n"
            "branches:\n"
            "\n"
            "1. **Credentialed URL** — a URL carrying userinfo, or one of\n"
            "   the known index-key markers.  Reported first and alone: this is\n"
            "   a secret in version control, and the credential must be rotated\n"
            "   before the lock is regenerated.\n"
            "2. **Non-PyPI host** — the lock resolves from an internal\n"
            "   mirror or proxy.  Restore the committed lock; if a dependency\n"
            "   genuinely changed, re-lock with\n"
            "   ``uv lock --default-index https://pypi.org/simple``.\n"
            "\n"
            "**Findings never quote a URL — only a host and a count.**  They\n"
            "reach SARIF, GitHub code scanning, CI logs and the remediation run\n"
            "artifacts, so interpolating a credentialed URL into a message would\n"
            "copy the secret into all four.\n"
            "\n"
            "**The finding is anchored on** ``pyproject.toml``, not on\n"
            "``uv.lock``, and names the lock in its message.  A lockfile is\n"
            "regenerated wholesale on every lock, so a\n"
            "``# conformance: ignore[D013]`` directive written into it would not\n"
            "survive — anchoring there would leave the rule with no\n"
            "suppression path at all.  ``D011``'s lock branch sets the same\n"
            "precedent.\n"
            "\n"
            "A lock that is absent or unparseable never manufactures a finding.\n"
            "A lock that parses but yields **no** URLs is reported on stderr as\n"
            "undetermined rather than passing silently: the URL spelling is a uv\n"
            "implementation detail, and a matcher that has gone inert must not\n"
            "read as a clean result.  Cite: FND-1928."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d013"
        ),
    ),
    RuleDefinition(
        id="D014",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — no `[tool.uv] exclude-newer` and no "
            "`exclude-newer-package`. That repo's release-age cooldown lives in "
            "renovate.json, which extends the SDK's shared preset: a rolling window "
            "bounds the lanes Renovate resolves itself, and the lock-refresh driver "
            "bounds its own re-resolve — leaving a human's `uv lock` and the CVE-fix "
            "workflow unfenced, which a date in pyproject.toml would not."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.BOTH,
        name="AbsoluteResolverFence",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="supply-chain",
        autofixable=True,
        since="0.31.0",
        rationale=(
            "A repo-local '[tool.uv] exclude-newer' pinned to a fixed date is written as a "
            "release-age cooldown and behaves as a freeze. The comment above it in all "
            "three repos found carrying one says 'never resolve a version published in the "
            "last 7 days'; the value is a timestamp, so it was seven days on the day it was "
            "typed and has widened by a day every day since. Nothing fails when nobody "
            "moves it, which is why it is found by census rather than by anyone noticing. "
            "It bounds every resolve in the repo, /fix-vulnerabilities included, so a "
            "security fix cannot land until a human edits the date first — the opposite of "
            "what a cooldown is for. It is also silent in a way that reads as health: "
            "Renovate's package datasource is unbounded, so it keeps opening upgrade PRs, "
            "and 'uv lock --upgrade-package' then returns the lock unchanged because uv "
            "cannot see past the fence. The repo looks maintained and is frozen. "
            "Customer impact: the connector ships on a dependency set nobody chose, missing "
            "SDK fixes and CVE patches alike, and the PRs that would have delivered them sit "
            "open and green-adjacent rather than failing visibly. Measured 2026-09-14: three "
            "fleet repos, the oldest fence 33 days stale, one of them 12 conformance minors "
            "and 4 SDK minors behind. FND-414 cleaned eleven repos of this in August; two of "
            "the three found in September were written AFTER that cleanup, which is why this "
            "is a rule and not another sweep."
        ),
        short_description=(
            "pyproject.toml pins [tool.uv] exclude-newer to a fixed date, "
            "freezing every resolve in the repo"
        ),
        full_description=(
            "The repo's root ``pyproject.toml`` must not fence uv's resolver\n"
            "to an absolute date.  Both places one can be declared are\n"
            "checked, and each yields its own finding::\n"
            "\n"
            "    [tool.uv]\n"
            '    exclude-newer = "2026-09-01T00:00:00Z"          # repo-wide\n'
            '    exclude-newer-package = { pkg = "2026-09-03" }  # per package\n'
            "\n"
            "Reading only the first misreports a repo that carries both —\n"
            "``atlan-mongodbatlas-app`` has a repo-wide fence three weeks\n"
            "older than the per-package carve-outs written to work around it.\n"
            "\n"
            "**The value's shape is what is graded, not the key's presence.**\n"
            'A duration genuinely rolls, so ``exclude-newer-span = "P3D"``\n'
            "and any non-date value pass.  Only a value beginning\n"
            "``YYYY-MM-DD`` — uv accepts a bare date and an RFC 3339\n"
            "timestamp — is a fence that never moves.\n"
            "\n"
            "**Each per-package fence carries a discriminator.**  Written as\n"
            "an inline table they share one line, so fingerprints — hashed\n"
            "from (rule, uri, line) — would collapse two fenced packages into\n"
            "one SARIF identity and a line-level directive would silence both.\n"
            "The discriminator is ``exclude-newer-package.<name>``, so one can\n"
            "be suppressed alone with\n"
            "``# conformance: ignore[D014:exclude-newer-package.<name>]`` while\n"
            "the repo-wide fence is still reported.  Written as a\n"
            "``[tool.uv.exclude-newer-package]`` sub-table each key anchors on\n"
            "its own line as well, so the directive sits where a reader expects\n"
            "it; the discriminator is set either way, because a fingerprint\n"
            "must not depend on which spelling the repo chose.\n"
            "\n"
            "**Findings never state how stale the fence is.**  Staleness is\n"
            "the point of the rule and also the one fact that changes on every\n"
            "run: a day count in the message would rewrite the SARIF, move the\n"
            "fingerprint and re-notify on an unchanged repo daily, forever.\n"
            "The message names the date; the reader subtracts.\n"
            "\n"
            "Deliberately not a mechanical rewrite.  Deleting the key is one line, but\n"
            "the next resolve then jumps the repo across every release the\n"
            "fence was holding back, and at least one instance is a documented\n"
            "owner-gated hold (FND-1125) rather than drift.  Which of those a\n"
            "given fence is cannot be read off the file, so the remediation\n"
            "loop must not decide it.\n"
            "\n"
            "Scope is ``both``: a fence bounds the SDK's own resolves exactly\n"
            "as it bounds an app's.  The fleet does need a release-age bound —\n"
            "it is applied centrally and rolling, by ``minimumReleaseAge`` in\n"
            "``renovate-config/default.json`` and by the bounded driver in\n"
            "``postUpgradeTasks``, neither of which can rot in a repo.\n"
            "Cite: FND-1985, FND-1999, FND-2000, FND-2001."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d014"
        ),
    ),
    RuleDefinition(
        id="D015",
        canonical_reference=(
            "atlan-openapi-app pyproject.toml — `[tool.pyright]` sets venvPath, venv, "
            "typeCheckingMode and two report levels, and declares no `exclude` at all, "
            "so pyright's built-in defaults stay in force and `**/.*` keeps .venv out "
            "of the walk. A repo that does need an exclude restates `**/.*` beside its "
            "own entries; atlan-mysql-app and atlan-metabase-app both exclude "
            "`.github/**` without it and are open findings, which is why neither is "
            "cited here."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.BOTH,
        name="PyrightExcludeClobbersDefaults",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="tooling-baseline",
        autofixable=True,
        since="0.32.0",
        rationale=(
            "pyright's 'exclude' REPLACES its built-in defaults rather than adding to "
            "them, and one of those defaults -- '**/.*' -- is the only thing keeping "
            ".venv out of the analysis. Nothing in the repo says so: the protection is "
            "incidental, earned because the directory happens to start with a dot. So "
            "the first time anyone excludes a path of their own -- '.github/**', "
            "'app/generated/**', a fixture tree -- the virtualenv silently joins the "
            "set of files pyright type-checks, and the edit that caused it looks "
            "entirely reasonable in review. "
            ".gitignore does not save it. pyright has no .gitignore integration at all; "
            "measured on 1.1.410 against a gitignored NON-dot directory, so the dot-dir "
            "default could not be the cause, pyright analysed it anyway. ruff does "
            "respect .gitignore, which is exactly why the wrong intuition is so common. "
            "The failure is invisible under every normal invocation. pre-commit passes "
            "filenames to the hook and an editor checks one file, so both stay fast; "
            "only a bare 'uv run pyright' walks the root. That is the invocation an "
            "agent types. Measured on one such run: argument-scoped invocations "
            "returned in 12-19s while two bare ones ran 553s and 912s without "
            "returning, the second exhausting the lane's 900s no-progress budget and "
            "escalating the whole run to a human. Customer impact is indirect -- no "
            "connector ships differently -- but a type checker nobody can afford to run "
            "is a gate that stops catching the contract mismatches it exists to catch. "
            "Measured 2026-09-17 across all 115 atlan-*-app repos: 73 declare an "
            "'exclude' that clobbers the defaults with no scoped 'include' to save "
            "them, 3 more are latent behind an 'include', and application-sdk itself is "
            "in the first group -- which is why the scope is 'both' and not 'app'."
        ),
        short_description=(
            "pyproject.toml declares [tool.pyright] exclude without restating the "
            "dot-directory default, so a bare pyright run walks .venv"
        ),
        full_description=(
            "``[tool.pyright].exclude`` **replaces** pyright's built-in\n"
            "defaults -- ``**/node_modules``, ``**/__pycache__`` and\n"
            "``**/.*`` -- rather than appending to them.  Losing the first\n"
            "two costs nothing in a Python repo.  Losing ``**/.*`` is what\n"
            "matters: it is the only reason ``.venv`` is not type-checked::\n"
            "\n"
            "    [tool.pyright]\n"
            '    exclude = [".github/**"]      # .venv is now in scope\n'
            "\n"
            "**Only the dot-directory protection is graded**, not all three\n"
            "defaults, so a repo that deliberately restates just ``**/.*``\n"
            "passes.  Either spelling clears the rule -- ``**/.*`` itself, or\n"
            "an explicit ``.venv``/``.venv/``/``.venv/**`` entry.\n"
            "\n"
            "**An empty list is still a clobber.**  ``exclude = []`` reads as\n"
            "a no-op and is not one: declaring the key replaces the defaults\n"
            "with nothing at all, which is the worst case rather than the\n"
            "neutral one.  It is reported like any other unprotected list.\n"
            "\n"
            "**A scoped ``include`` is a complete defence and is honoured.**\n"
            "With ``include`` set, pyright only ever walks the listed roots\n"
            "and never reaches ``.venv``, so ``exclude`` cannot matter and no\n"
            "finding is raised.  This is a real pattern in the fleet, not a\n"
            "hypothetical -- three repos rely on it.\n"
            "\n"
            "``ignore`` does **not** clear the rule.  It suppresses\n"
            "diagnostics for matched files but still parses them, so it does\n"
            "nothing for the walk cost that is the entire problem.\n"
            "\n"
            "Graded on ``pyproject.toml`` only.  A repo configuring pyright\n"
            "through ``pyrightconfig.json`` is out of scope: the D-series CI\n"
            "leg watches ``**/pyproject.toml``, and a checker must not read a\n"
            "tree its leg's path filter does not watch.\n"
            "\n"
            "Autofixable: the remedy is to append the three defaults to the\n"
            "existing list, preserving the repo's own entries.  The\n"
            "prescription never introduces an ``include`` key -- scoping\n"
            "``include`` is a legitimate alternative a human may choose, but\n"
            "it changes *what gets type-checked*, which is not a safe\n"
            "automatic rewrite.\n"
            "\n"
            "Scope is ``both``: application-sdk's own ``pyproject.toml``\n"
            "carries this exact shape.  Cite: FND-2229."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dependency.md#d015"
        ),
    ),
)

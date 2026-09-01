"""K-series rules — contract-toolkit conformance (BLDX-1479).

These rules flag app repos that still amend the legacy ``NativeApp.pkl`` or
``NativeAppBundle.pkl`` contract base modules instead of the canonical
``App.pkl`` module introduced in contract-toolkit v0.10.0, and that carry
NativeApp-only knobs that ``App.pkl`` dropped entirely.

Background
----------
The contract-toolkit ships three contract base modules.  ``App.pkl`` (v0.10.0+)
is the single entry point that supersedes ``NativeApp.pkl`` (single-entrypoint)
and ``NativeAppBundle.pkl`` (multi-entrypoint bundle).  The legacy modules are
"frozen — not used by new contracts" per the toolkit README and are slated for
hard removal at toolkit v1.0.

When renovate bumps the toolkit version and regenerates, contracts that still
amend the legacy modules silently break or emit stale artifacts.  Faizan Shaik
observed (2026-06-23 Slack thread) that "way too many apps currently use
``flatManifestArg = true`` argument and many outdated APIs."

K001 catches the wrong *base module*; K002 catches the *legacy APIs* that exist
only in ``NativeApp.pkl`` and have no counterpart in ``App.pkl``.

Generated-artifact freshness (BLDX-1414)
----------------------------------------
K003/K004/K005 guard the *outputs* of ``pkl eval`` rather than the ``.pkl``
source: they catch an app whose committed generated artifacts (``atlan.yaml``,
``app/generated/**``) have drifted from what regenerating today would produce.
These are deterministic *proxy* signals — a stale lock (K003), a missing output
(K004), or a stripped provenance banner (K005). They cannot prove full content
freshness (a hand-edit that keeps the banner is invisible to a static scanner);
that guarantee belongs to the CI regenerate-and-diff freshness gate. All three
are APP-scoped and no-op on any repo without a ``contract/`` directory. K003 is
BLOCK — a pin that disagrees with its lock means the committed artifacts were
generated from a toolkit version the contract no longer claims, which is the
route the K009/K011 customer-facing breakages travel; K004 and K005 stay WARN as
hygiene proxies.

Manifest-vs-contract field validation (BLDX-1527)
--------------------------------------------------
K006 closes a different structural gap: ``App.pkl``'s pipeline nodes (e.g.
``PublishNode``) unconditionally wire a downstream node's args to the
entrypoint's runtime output via a JSONPath such as
``$.extract.outputs.publish_state_prefix``. Pkl compiles this before any
Python runs and has zero visibility into the app's ``Output`` model; the B005
contract-ledger checker only knows a field "was tracked and disappeared," with
no knowledge of the manifest's JSONPath requirements. An app can silently
delete a field the manifest depends on and nothing static catches it — only a
rarely-run, non-deterministic full-DAG e2e does (the incident that motivated
this rule: ``OpenAPIConnectorOutput`` lost ``publish_state_prefix`` /
``current_state_prefix`` in a cleanup PR and went undetected for ~12 days).
K006 cross-references every ``$.extract.outputs.<field>`` reference in the
committed ``app/generated/**/manifest.json`` against the entrypoint's Python
``Output`` contract, resolved across its full inheritance chain (so a field
supplied by an SDK mixin such as ``PublishInputMixin`` counts as declared).
WARN and APP-scoped; no-op on any repo without ``app/generated/``.

Release-readiness guards (CONNECT release-pipeline)
---------------------------------------------------
K011/K012 guard the two pieces of contract-toolkit setup the marketplace
publish pipeline depends on but that no other check enforced — each has
silently broken a real semver release:

* K011 — the generated ``atlan.yaml`` must carry a top-level ``app_id``. It
  maps the app to its Global Marketplace record; the release publish step
  (``build-and-publish-app.yaml`` → ``.github/scripts/parse_atlan_yaml.py``)
  POSTs it to the GM, and an empty value makes the GM return 404, so the
  release is cut but never appears in the marketplace. This regressed when an
  app's ``atlan.yaml`` became fully pkl-generated and the ``app_id`` — which
  had been hand-carried in the file — was dropped because the pkl ``metadata``
  block never declared it.
* K012 — ``pyproject.toml`` must define a ``generate`` poe task. The SDK
  Certify step runs ``uv run poe generate`` and hard-fails when the task is
  absent, aborting the publish before it starts (``Unrecognized task
  'generate'``). Apps that defined generation only in a ``Makefile`` had no
  such task.

Both are BLOCK-tier: unlike the WARN hygiene rules above, a violation here does
not degrade quality — it silently prevents the app from ever reaching the
marketplace, so it must fail the PR that introduces it rather than surface at
release time.

Scope
-----
``APP`` only: consumer apps have a ``contract/`` directory; the SDK itself does
not.  Both rules no-op on the SDK repo (same guard as P016/P025 — the runner
auto-detects scope from ``[project].name`` in ``pyproject.toml``).

Suppression
-----------
Both rules are WARN-tier and can be suppressed with a pkl-style directive:

    // conformance: ignore[K001] intentional: phased migration tracked in BLDX-XXXX

The ``//`` prefix is pkl's line comment syntax.  The directive grammar mirrors the
Python ``# conformance: ignore[...]`` form: the rule-id list is optional (omitting
it suppresses any rule on that line), and justification text is mandatory.
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
        id="K001",
        scope=RuleScope.APP,
        name="ContractAmendsLegacyModule",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.9.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "App.pkl (contract-toolkit v0.10.0+) is the canonical single entry "
            "point that supersedes NativeApp.pkl (single-entrypoint) and "
            "NativeAppBundle.pkl (multi-entrypoint bundle).  Legacy modules are "
            "frozen and slated for hard removal at toolkit v1.0.  Renovate "
            "version bumps + regeneration break contracts that still amend the "
            "legacy base, and the generated artifacts (atlan.yaml, manifest.json) "
            "may silently carry stale or incorrectly structured fields.  "
            "Migrating to App.pkl before v1.0 removes the blast radius of the "
            "hard cutover and aligns every app with the one supported workflow "
            "for contract evolution (BLDX-1479)."
        ),
        short_description=(
            "Contract amends NativeApp.pkl or NativeAppBundle.pkl — migrate to App.pkl"
        ),
        full_description=(
            "The ``contract/app.pkl`` file (or any ``contract/**/*.pkl`` file) "
            "contains an ``amends`` line pointing at ``NativeApp.pkl`` or "
            "``NativeAppBundle.pkl`` instead of the canonical ``App.pkl``.\n"
            "\n"
            "``App.pkl`` (contract-toolkit v0.10.0+) is the single entry point "
            "for all Atlan native apps — both single-entrypoint and "
            "multi-entrypoint (bundle) contracts.  The legacy modules are frozen "
            "(no new features) and are scheduled for hard removal at v1.0.\n"
            "\n"
            "**Migration steps:**\n"
            "\n"
            "1. Change the ``amends`` line to "
            '``amends "@app-contract-toolkit/App.pkl"``.\n'
            "\n"
            "2. Resolve ``workflowType``.  NativeApp.pkl paired a PascalCase "
            "``workflowType`` with an optional ``workflowTypeOverride`` and "
            "applied automatic kebab-casing.  App.pkl takes a **verbatim** "
            "string — set it to the exact string the legacy contract would have "
            "emitted (apply PascalCase→kebab-case manually if needed), then drop "
            "``workflowTypeOverride``.  Omit ``workflowType`` entirely when it "
            "would equal the ``name`` field (App.pkl defaults to kebab-casing "
            "``name``).\n"
            "\n"
            "3. Make ``connector`` nullable if it is a utility app with no "
            "connector type (NativeApp.pkl required ``connector``; App.pkl allows "
            "``null``).\n"
            "\n"
            "4. For ``NativeAppBundle.pkl`` migrations: move each per-entrypoint "
            "contract into App.pkl's typed ``entrypoints`` block.  Each child "
            "contract file that also amends ``NativeApp.pkl`` will produce its "
            "own K001 finding and must be migrated separately.\n"
            "\n"
            "5. Run ``pkl eval -m . contract/app.pkl`` (or ``uv run poe "
            "generate``) to regenerate ``app/generated/**`` and ``atlan.yaml``.  "
            "Never hand-edit generated artifacts — K004/K005 and the "
            "generated-artifact freshness gate catch staleness.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K001] <reason>`` on the "
            "``amends`` line or the comment-only line directly above it when a "
            "deliberate delay is justified (e.g. phased migration tracked in a "
            "follow-on ticket).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k001"
        ),
    ),
    RuleDefinition(
        id="K002",
        scope=RuleScope.APP,
        name="LegacyContractApi",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.9.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "Several properties and imports that exist in NativeApp.pkl were "
            "intentionally dropped when App.pkl was designed: flatManifestArgs and "
            "manifestMetadataArgs (App.pkl always emits flat top-level args), "
            "workflowTypeOverride (App.pkl takes a verbatim workflowType), and "
            "explicit imports of Config.pkl/Credential.pkl/Renderers.pkl "
            "(Argo-era modules App.pkl no longer uses).  "
            "Their presence in a contract that claims to amend App.pkl (or that "
            "will be migrated to App.pkl) indicates that the migration is "
            "incomplete.  When any of these knobs remain after the amends line is "
            "changed, pkl eval fails, blocking CI (BLDX-1479)."
        ),
        short_description=(
            "Contract uses NativeApp-only APIs (flatManifestArgs, "
            "workflowTypeOverride, or legacy imports) removed in App.pkl"
        ),
        full_description=(
            "The ``contract/**/*.pkl`` file contains one or more "
            "NativeApp-only properties or imports that do not exist in "
            "``App.pkl``:\n"
            "\n"
            "* ``flatManifestArgs`` / ``manifestMetadataArgs`` — control how "
            "workflow params are nested in the manifest ``args`` object.  "
            "App.pkl always emits flat top-level args; these flags have no "
            "counterpart and must be removed.\n"
            "\n"
            "* ``workflowTypeOverride`` — companion to NativeApp.pkl's "
            "PascalCase ``workflowType`` field.  App.pkl takes a verbatim "
            "string; resolve the final kebab-cased value and set it as "
            "App.pkl's ``workflowType``, then remove ``workflowTypeOverride``.\n"
            "\n"
            '* ``import "…Config.pkl"`` — Config.pkl provides widget types '
            "for NativeApp.pkl.  App.pkl re-exports all widget types as "
            "typealiases (``TextInput``, ``UIConfig``, etc.) — remove the "
            "import and switch ``Config.UIConfig`` → ``UIConfig``, "
            "``Config.TextInput`` → ``TextInput``, etc.\n"
            "\n"
            '* ``import "…Credential.pkl"`` and ``import "…Renderers.pkl"`` '
            "(Argo-era modules) — no longer used by App.pkl; remove both.\n"
            "\n"
            'Note: ``import "…Connectors.pkl"`` is **not** flagged — the '
            "Connectors registry is still imported explicitly by App.pkl "
            "consumers (App.pkl imports it internally and types ``connector`` as "
            "``Connectors.Type`` but does not re-export the constants), so the "
            "import is required, not legacy.\n"
            "\n"
            "Note: a contract that **amends** ``Credential.pkl`` (a "
            "credential-config sub-contract, not an App.pkl entrypoint) is "
            '**not** flagged for its ``import "…Config.pkl"`` line.  '
            "Credential.pkl uses ``Config.*`` internally but, unlike App.pkl, "
            "does not re-export the widget types as unqualified typealiases, so "
            "such a contract genuinely needs the import for ``pkl eval`` to "
            "resolve ``Config.TextInput`` etc. — flagging it would be a false "
            "positive.  The exemption covers ``Config.pkl`` only: a credential "
            'contract carrying ``import "…Renderers.pkl"`` or '
            '``import "…Credential.pkl"`` is still legacy and still fires.\n'
            "\n"
            "**Note:** if the contract also has a K001 finding (still amending a "
            "legacy module), address K001 first — many K002 knobs disappear "
            "automatically when the module changes, because App.pkl simply "
            "lacks those properties.\n"
            "\n"
            "After editing, run ``pkl eval -m . contract/app.pkl`` (or "
            "``uv run poe generate``) to regenerate ``app/generated/**`` and "
            "``atlan.yaml``.  Never hand-edit generated artifacts — K004/K005 "
            "and the generated-artifact freshness gate catch staleness.\n"
            "\n"
            "**Scanner limitation:** the checker is not string-literal aware.  "
            "A property name that appears only inside a string literal "
            '(e.g. ``description = "flatManifestArgs is removed in App.pkl"`` '
            "on a single line) may be flagged.  Use "
            "``// conformance: ignore[K002] <reason>`` to suppress false "
            "positives; that directive is the intended workaround for any case "
            "where the pattern matches non-code content.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K002] <reason>`` on "
            "the violating line or the comment-only line directly above it.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k002"
        ),
    ),
    RuleDefinition(
        id="K003",
        scope=RuleScope.APP,
        name="ContractLockDrift",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.9.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "contract/PklProject pins each pkl dependency (e.g. app-contract-toolkit) "
            "at an exact @<version>; contract/PklProject.deps.json is the resolved "
            "lock that records the version pkl actually fetched, with its checksum.  "
            "When someone bumps the pin in PklProject but does not re-resolve, the "
            "lock stays behind: pkl eval regenerates from the OLD toolkit, so the "
            "committed generated artifacts silently reflect a version the contract no "
            "longer claims.  The self-hosted Renovate runner keeps these two in sync "
            "on bot bumps (regenerating the lock and artifacts in the same PR via "
            "postUpgradeTasks), but a manual pin edit bypasses it entirely.  Comparing "
            "the two files is a pure, deterministic text check that needs no pkl "
            "toolchain, so it catches the drift the moment it lands (BLDX-1414). "
            "Customer impact: the artifacts the customer installs were generated from "
            "a toolkit version the contract no longer claims, so the manifest, "
            "contract and marketplace record they receive can each be a version behind "
            "what was reviewed — this is the gap the K009 and K011 breakages reach "
            "customers through, and it hides them by making the committed artifacts "
            "look freshly generated."
        ),
        short_description=(
            "contract/PklProject pin does not match the resolved version in "
            "PklProject.deps.json — re-resolve the lock"
        ),
        full_description=(
            "A dependency pinned in ``contract/PklProject`` resolves to a "
            "different version in ``contract/PklProject.deps.json`` (or the lock "
            "file is missing / does not contain the dependency at all).  The lock "
            "is stale relative to the pin.\n"
            "\n"
            "``pkl eval`` generates ``app/generated/**`` and ``atlan.yaml`` from "
            "whatever the lock resolves to — so a stale lock means the committed "
            "artifacts were generated from a toolkit version the contract no longer "
            "pins.  A bump to the ``@<version>`` in ``PklProject`` must be paired "
            "with a re-resolve.\n"
            "\n"
            "**Fix:** re-resolve the Pkl project so the lock matches the pin, then "
            "regenerate:\n"
            "\n"
            "    pkl project resolve   # rewrites contract/PklProject.deps.json\n"
            "    pkl eval -m . contract/app.pkl   # regenerates the artifacts\n"
            "\n"
            "On a ``renovate/**`` branch the self-hosted Renovate runner does this "
            "automatically in the same PR (via postUpgradeTasks); on a manual bump "
            "run the commands above (or ``uv run poe generate`` where the app "
            "defines it).\n"
            "\n"
            "The version match is prefix-aware: a broad pin such as ``@0`` is "
            "satisfied by any resolved ``0.y.z`` and is never flagged — only a "
            "fully-specified pin (``@0.16.0``) that disagrees with the lock, or a "
            "lock that lacks the dependency, is a finding.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K003] <reason>`` on the "
            "``uri`` line in ``contract/PklProject`` (or the comment-only line "
            "directly above it) when a deliberate lag is justified.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k003"
        ),
    ),
    RuleDefinition(
        id="K004",
        scope=RuleScope.APP,
        name="MissingGeneratedArtifact",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.9.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "An app that defines contract/app.pkl commits the pkl eval outputs "
            "(atlan.yaml, app/generated/manifest.json, app/generated/_input.py) so "
            "that deployment and CI consume them without a pkl toolchain.  When one "
            "of those outputs is absent while the contract exists, the app was never "
            "generated (or the artifact was deleted): the platform reads a manifest "
            "that does not exist, and the app fails to deploy or register.  File "
            "existence is a fully deterministic check that needs no pkl (BLDX-1414)."
        ),
        short_description=(
            "contract/app.pkl exists but an expected generated artifact "
            "(atlan.yaml / manifest.json / _input.py) is missing — regenerate"
        ),
        full_description=(
            "The app defines ``contract/app.pkl`` but one or more of the "
            "artifacts ``pkl eval`` is expected to produce is absent:\n"
            "\n"
            "* ``atlan.yaml``\n"
            "* ``app/generated/manifest.json``\n"
            "* ``app/generated/_input.py``\n"
            "\n"
            "These are the outputs the deployment pipeline and the SDK read at "
            "runtime; a missing one means the contract was never generated (or an "
            "output was deleted).\n"
            "\n"
            "**Fix:** regenerate from the contract —\n"
            "\n"
            "    pkl eval -m . contract/app.pkl\n"
            "\n"
            "(or ``uv run poe generate`` where the app defines it) and commit the "
            "result.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K004] <reason>`` on the "
            "``amends`` line of ``contract/app.pkl`` when an output is legitimately "
            "not produced for this app.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k004"
        ),
    ),
    RuleDefinition(
        id="K005",
        scope=RuleScope.APP,
        name="GeneratedArtifactBannerStripped",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.9.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "Every text artifact the contract toolkit emits carries a provenance "
            "banner in its first lines — 'AUTO-GENERATED from contract/app.pkl — DO "
            "NOT EDIT MANUALLY' (or the '… via contract-toolkit. DO NOT EDIT' "
            "variant).  A generated file that is MISSING that banner has almost "
            "always been hand-authored or hand-edited in place, which means it will "
            "silently diverge from the contract on the next regeneration.  This is a "
            "heuristic proxy, not a proof: a hand-edit that preserves the banner is "
            "invisible to a static scanner (only the CI regenerate-and-diff gate "
            "catches that).  Because a deliberately hand-maintained app legitimately "
            "strips the now-untrue banner, K005 stays WARN and is suppressed per file "
            "rather than ever graduating to BLOCK (BLDX-1414)."
        ),
        short_description=(
            "A generated text artifact (atlan.yaml / app/generated/*.py) is missing "
            "its AUTO-GENERATED provenance banner — likely hand-edited"
        ),
        full_description=(
            "A file the contract toolkit is expected to generate "
            "(``atlan.yaml``, ``app.yaml``, or a ``.py`` file under "
            "``app/generated/`` other than ``__init__.py``) does not carry the "
            "provenance banner the toolkit stamps into the first lines of every "
            "output it writes:\n"
            "\n"
            "    # AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
            "\n"
            "(or the ``# Generated from contract/app.pkl via contract-toolkit. DO "
            "NOT EDIT.`` variant).  A missing banner means the file was authored or "
            "edited by hand and will diverge from the contract the next time "
            "``pkl eval`` runs.\n"
            "\n"
            "``.json`` artifacts (``manifest.json`` etc.) are exempt — JSON has no "
            "comment syntax to carry a banner — as is the empty "
            "``app/generated/__init__.py``.\n"
            "\n"
            "**Fix:** regenerate from the contract (``pkl eval -m . "
            "contract/app.pkl``) so the file is re-emitted with its banner.\n"
            "\n"
            "**Limitation:** this rule cannot see content-level hand-edits that "
            "leave the banner intact; the CI generated-artifact freshness gate "
            "(regenerate-and-diff) is the check that proves full freshness.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K005] <reason>`` on the "
            "first line of the file (or the line above it) for an app that "
            "deliberately hand-maintains this artifact.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k005"
        ),
    ),
    RuleDefinition(
        id="K006",
        scope=RuleScope.APP,
        name="ManifestContractFieldMismatch",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.13.0",
        orthogonal_gate="tests",
        rationale=(
            "App.pkl's pipeline nodes (e.g. PublishNode) unconditionally wire a "
            "downstream node's args to the entrypoint's runtime output via a "
            "JSONPath such as $.extract.outputs.publish_state_prefix. Pkl compiles "
            "this before any Python runs and has zero visibility into the app's "
            "Output model; the B005 contract-ledger checker only knows a field 'was "
            "tracked and disappeared,' with no knowledge of the manifest's JSONPath "
            "requirements. An app can therefore silently delete a field the manifest "
            "depends on, and nothing static catches it — only a rarely-run, "
            "non-deterministic full-DAG e2e run against a real Automation Engine "
            "does. This is exactly what happened when OpenAPIConnectorOutput lost "
            "publish_state_prefix and current_state_prefix in an unrelated "
            "conformance-cleanup PR and went undetected for about 12 days (BLDX-1527). "
            "K006 closes the loop with a structural manifest-vs-contract diff, "
            "computed once both artifacts exist, without either layer needing "
            "visibility into the other's language."
        ),
        short_description=(
            "app/generated/**/manifest.json references an "
            "$.extract.outputs.<field> the entrypoint's Output contract "
            "does not declare"
        ),
        full_description=(
            "A ``$.extract.outputs.<field>`` JSONPath reference in a committed "
            "``app/generated/**/manifest.json`` DAG node's ``inputs.args`` names a "
            "field that the corresponding entrypoint's Python ``Output`` contract "
            "does not declare — not directly, and not via any inherited base class "
            "or SDK mixin.\n"
            "\n"
            "The Automation Engine resolves this JSONPath at runtime against the "
            "object the entrypoint's workflow actually returned. A missing field "
            "means the reference never resolves, and the dependent pipeline step "
            "(most commonly the default ``publish`` step) fails at runtime with an "
            "unresolved-JSONPath error — the one signal that would have caught this "
            "is a rarely-run, opt-in-labeled, non-deterministic full-DAG e2e test.\n"
            "\n"
            "**Fix:** declare the missing field(s) on the entrypoint's ``Output`` "
            "model, or mix in the SDK contract base that already supplies them. For "
            "the publish-state fields specifically "
            "(``connection_qualified_name``, ``transformed_data_prefix``, "
            "``publish_state_prefix``, ``current_state_prefix``), mix in "
            "``application_sdk.contracts.base.PublishInputMixin`` rather than "
            "hand-declaring each field — it also derives the values correctly from "
            "``connection_qualified_name``.\n"
            "\n"
            "**Never hand-edit** ``app/generated/manifest.json`` to work around a "
            "finding — it is a ``pkl eval`` output (K004/K005 and the generated-"
            "artifact freshness gate catch a hand-edited manifest). If the "
            "referenced field is genuinely not needed (e.g. the pipeline step that "
            "consumes it should not be enabled), remove or reconfigure that step in "
            "``contract/app.pkl`` and re-run ``pkl eval -m . contract/app.pkl`` "
            "instead.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K006] <reason>`` on the "
            "``Output`` class definition (or the comment-only line directly above "
            "it) when a mismatch is understood and deliberately deferred.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k006"
        ),
    ),
    RuleDefinition(
        id="K007",
        scope=RuleScope.APP,
        name="ToolkitVersionOutdated",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.12.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "The app-contract-toolkit ships fixes and new contract capabilities in "
            "every release; an app pinned to an old version regenerates stale "
            "artifacts, misses schema changes, and is the usual root cause of "
            "leftover scaffold placeholders (K009) and legacy-API drift (K002). The "
            "latest published version is read from the baked-in toolkit baseline "
            "(data/toolkit_baseline.json), regenerated from the toolkit's own "
            "PklProject and guarded against drift in CI, so the check stays correct "
            "offline inside any consumer repo."
        ),
        short_description=(
            "app-contract-toolkit dependency resolves to a version below the latest "
            "published one — bump and regenerate"
        ),
        full_description=(
            "The ``app-contract-toolkit`` dependency in ``contract/PklProject`` "
            "resolves (per ``contract/PklProject.deps.json``) to a version older "
            "than the latest the SDK publishes.\n"
            "\n"
            "The check uses the resolved lock as the ground truth for the version "
            "actually in use; a missing or stale lock is K003's concern, so K007 "
            "stays quiet in that case rather than guessing from a broad pin.\n"
            "\n"
            "**Fix:** bump the ``@<version>`` in ``contract/PklProject`` to the "
            "latest, run ``pkl project resolve`` to refresh the lock, then "
            "regenerate with ``pkl eval -m . contract/app.pkl``. On a "
            "``renovate/**`` branch the self-hosted Renovate runner does this "
            "automatically via postUpgradeTasks.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K007] <reason>`` on the "
            "``uri`` line (or the comment-only line directly above it) when a "
            "deliberate lag is justified.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k007"
        ),
    ),
    RuleDefinition(
        id="K008",
        scope=RuleScope.APP,
        name="ToolkitSourceNonCanonical",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.12.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "Every app must consume the app-contract-toolkit from the single "
            "SDK-published package so the whole fleet generates from the same "
            "contract semantics and receives version bumps through the standard "
            "renovate flow. A dependency pointed at a fork, a local file path, or a "
            "different host silently diverges — it can pin behavior the SDK no "
            "longer supports and is invisible to the version floor (K007), which "
            "only compares against the canonical package. The canonical base URI is "
            "read from the baked-in toolkit baseline."
        ),
        short_description=(
            "app-contract-toolkit is sourced from a non-canonical base URI (fork / "
            "local path / wrong host)"
        ),
        full_description=(
            "The ``app-contract-toolkit`` dependency in ``contract/PklProject`` is "
            "pointed at a base URI other than the canonical SDK-published package "
            "(``package://atlanhq.github.io/application-sdk/contracts/"
            "app-contract-toolkit``).\n"
            "\n"
            'The dependency is identified by its ``["app-contract-toolkit"]`` '
            "mapping key (falling back to any URI whose path segment is "
            "``app-contract-toolkit``), so a fork under the canonical key is still "
            "caught.\n"
            "\n"
            "**Fix:** point the dependency at the canonical package "
            "``package://atlanhq.github.io/application-sdk/contracts/"
            "app-contract-toolkit@<latest>`` and run ``pkl project resolve``.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K008] <reason>`` on the "
            "``uri`` line (or the comment-only line directly above it).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k008"
        ),
    ),
    RuleDefinition(
        id="K009",
        scope=RuleScope.APP,
        name="UnresolvedScaffoldPlaceholder",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.12.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "A committed generated artifact that still contains a single-brace "
            "scaffold token such as {app_name} is shipping template text where a "
            "rendered value belongs — the current toolkit resolves {app_name} to the "
            "app's literal name, so a leftover means the artifact was generated by an "
            "outdated toolkit (or a legacy NativeApp.pkl base) and is objectively "
            "wrong on the wire, not merely stylistically stale. Because it is never a "
            "false positive (the {{...}} double-brace runtime tokens are excluded, "
            "and the one legitimate single-brace token {deployment_name} is not in "
            "the flagged set), this is a BLOCK-tier rule rather than the usual "
            "land-as-WARN default: the only correct resolution is to upgrade to the "
            "latest app-contract-toolkit and regenerate. "
            "Customer impact: the literal template token ships to the tenant on the "
            "wire — artifacts get rooted under a path segment named '{app_name}' or the "
            "marketplace record carries template text, so the customer's install or crawl "
            "fails on identity plumbing they can neither see nor fix."
        ),
        short_description=(
            "Generated artifact contains an unresolved single-brace scaffold "
            "placeholder ({app_name}, {name}, …) — upgrade the toolkit and regenerate"
        ),
        full_description=(
            "A committed generated artifact (``atlan.yaml``, ``app.yaml``, or a file "
            "under ``app/generated/``) contains a single-brace scaffold placeholder "
            "token — ``{app_name}``, ``{name}``, ``{app-name}``, "
            "``{entrypoint_name}``, ``{connection_name}``, and related scaffold "
            "vars.\n"
            "\n"
            "These are filled in by ``pkl eval``; a literal leftover means the "
            "artifact was generated by an outdated toolkit (or a legacy "
            "``NativeApp.pkl`` base) that never rendered the field. The current "
            "toolkit resolves ``{app_name}`` to the app's literal name, so this "
            "artifact is wrong as shipped — hence BLOCK, not WARN.\n"
            "\n"
            "Intentional ``{{credential}}`` / ``{{connection}}`` double-brace E2E "
            "runtime-substitution tokens are excluded, as is the legitimate "
            "``{deployment_name}`` deploy-time token the current toolkit still emits "
            "verbatim; none of these are ever flagged. In a YAML artifact, a token "
            "appearing only inside a ``#`` comment — e.g. an explanatory note "
            "documenting a runtime URL template such as "
            "``/workflows/v1/manifest?entrypoint={name}`` — is prose, not a "
            "leftover, and is likewise never flagged.\n"
            "\n"
            "**Fix (required):** upgrade ``app-contract-toolkit`` to the latest "
            "published version (see K007), migrate off any legacy ``NativeApp.pkl`` "
            "base (see K001), and regenerate with ``pkl eval -m . contract/app.pkl``. "
            "Never hand-edit the artifact to delete the token.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K009] <reason>`` on the "
            "placeholder's line (or the comment-only line directly above it) — "
            "available for text artifacts; a placeholder in a ``.json`` output has "
            "no comment syntax to suppress and must be regenerated.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k009"
        ),
    ),
    RuleDefinition(
        id="K010",
        scope=RuleScope.APP,
        name="E2EScaffoldingMissing",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.12.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "The toolkit emits app/generated/_e2e_base.py for every "
            "single-entrypoint app, and the SDK's E2E framework imports it; a "
            "missing file means the contract was never generated (or the output was "
            "deleted), so the app's E2E tests cannot resolve their typed base. "
            "Multi-entrypoint bundles emit E2E scaffolding into per-entrypoint "
            "subfolders instead, so the rule only applies when the contract "
            "declares no entrypoints block."
        ),
        short_description=(
            "Single-entrypoint contract/app.pkl exists but generated "
            "app/generated/_e2e_base.py is missing"
        ),
        full_description=(
            "A single-entrypoint ``contract/app.pkl`` exists but its generated E2E "
            "scaffolding ``app/generated/_e2e_base.py`` is absent. The toolkit emits "
            "this module unconditionally for single-entrypoint apps, and "
            "``application_sdk.testing.e2e`` imports it as the typed E2E base.\n"
            "\n"
            "Contracts that declare an ``entrypoints`` block (multi-entrypoint "
            "bundles) are out of scope — their E2E scaffolding lands in "
            "per-entrypoint subfolders, not the single-entrypoint path.\n"
            "\n"
            "**Fix:** regenerate with ``pkl eval -m . contract/app.pkl`` and commit "
            "the result.\n"
            "\n"
            "**Suppress** with ``// conformance: ignore[K010] <reason>`` on the "
            "``amends`` line of ``contract/app.pkl`` when the app legitimately ships "
            "no E2E scaffolding.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k010"
        ),
    ),
    RuleDefinition(
        id="K011",
        scope=RuleScope.APP,
        name="AppIdMissingFromContract",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.14.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "app_id is the stable identity that maps the app to its Global "
            "Marketplace record. The release publish step "
            "(build-and-publish-app.yaml -> .github/scripts/parse_atlan_yaml.py) "
            "reads the top-level app_id out of the generated atlan.yaml and POSTs "
            "it to the GM; an empty value makes the GM return 404 creating the "
            "version, so the GitHub Release is cut and the image is pushed to "
            "GHCR but the version never appears in the marketplace. This is a "
            "silent, release-time-only failure: nothing in the app's own PR CI "
            "notices, and it stays broken across every subsequent release until "
            "someone reads the failed publish log. It regressed on a live "
            "connector when atlan.yaml became fully pkl-generated and the app_id, "
            "previously hand-carried in the file, was dropped because the pkl "
            "metadata block never declared it. BLOCK-tier because the only "
            "outcome of shipping without it is a broken release. "
            "Customer impact: the fix a customer is waiting on looks shipped from the "
            "inside (tag cut, image pushed) but never appears in the marketplace they "
            "install from — the customer stays on the broken version while everyone "
            "believes the release went out."
        ),
        short_description=(
            "atlan.yaml is present but declares no top-level app_id — the "
            "marketplace publish will 404"
        ),
        full_description=(
            "The committed ``atlan.yaml`` has no top-level ``app_id:`` key. "
            "``app_id`` is the app's Global Marketplace identity; the release "
            "publish step POSTs it to the GM, and an empty value returns a 404 so "
            "the release never reaches the marketplace even though the tag, the "
            "GitHub Release, and the GHCR image all succeed.\n"
            "\n"
            "``atlan.yaml`` is generated from ``contract/app.pkl`` and must not be "
            "hand-edited (K005 guards its provenance banner). Restore ``app_id`` "
            "at its source, in the pkl ``metadata`` block — its entries are "
            "emitted as top-level ``atlan.yaml`` keys, exactly as "
            "``release_model`` already is:\n"
            "\n"
            "    metadata {\n"
            '      ["release_model"] = "semver"\n'
            '      ["app_id"] = "<your-app-uuid>"\n'
            "    }\n"
            "\n"
            "Find ``<your-app-uuid>`` in the Global Marketplace admin UI (the app "
            "URL — ``/admin/#/apps/<app_id>/versions``) or in the ``app_id`` of a "
            "prior successful publish log. Then regenerate with ``pkl eval -m . "
            "contract/app.pkl`` (or ``uv run poe generate``) and commit the "
            "updated ``atlan.yaml``.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K011] <reason>`` on the "
            "first line of ``atlan.yaml`` (or the line above) — but a suppression "
            "is almost never correct here: a semver app with no ``app_id`` cannot "
            "publish. It exists only for the rare non-published app that still "
            "ships an ``atlan.yaml``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k011"
        ),
    ),
    RuleDefinition(
        id="K012",
        scope=RuleScope.APP,
        name="GeneratePoeTaskMissing",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.14.0",
        orthogonal_gate="tests",
        rationale=(
            "The SDK build-and-publish Certify step runs ``uv run poe generate`` "
            "to prove the committed generated artifacts match what regenerating "
            "the contract today would produce, and it hard-fails (exit 1) when "
            "the task does not exist -- ``Error: Unrecognized task 'generate'``. "
            "That aborts Certify before the publish job runs, so a missing task "
            "blocks every marketplace release. Apps whose contract generation "
            "lived only in a Makefile target (``make generate``) had no poe task "
            "of that name, so the check passed locally yet the release died in "
            "CI. A one-line poe alias mirroring the Makefile target closes the "
            "gap. BLOCK-tier because, like K011, the only outcome of the missing "
            "piece is a broken release rather than degraded quality. "
            "Customer impact: every marketplace release of the app is dead on arrival "
            "until someone reads the failed Certify log — including the urgent one that "
            "carries a fix a customer is actively blocked on."
        ),
        short_description=(
            "pyproject.toml defines no [tool.poe.tasks.generate] task — the SDK "
            "Certify step will abort the publish"
        ),
        full_description=(
            "``pyproject.toml`` has a contract (a ``contract/`` directory exists) "
            "but ``[tool.poe.tasks]`` defines no ``generate`` task. The SDK "
            "Certify step (``build-and-publish-app.yaml``) runs ``uv run poe "
            "generate`` and fails the whole publish run with ``Unrecognized task "
            "'generate'`` when the task is absent.\n"
            "\n"
            "Add a ``generate`` poe task that regenerates the contract artifacts, "
            "mirroring the repo's ``Makefile`` target — for the common "
            "single-contract layout:\n"
            "\n"
            "    [tool.poe.tasks]\n"
            '    generate = "pkl eval --project-dir contract -m . '
            'contract/app.pkl"\n'
            "\n"
            "Include every ``.pkl`` the ``Makefile`` evaluates (e.g. a credential "
            "contract such as ``contract/csa-connectors-objectstore.pkl``) so "
            "``poe generate`` and ``make generate`` stay equivalent. Verify with "
            "``uv run poe generate`` — it must succeed and leave the generated "
            "tree unchanged.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K012] <reason>`` on the "
            "``[tool.poe.tasks]`` header line (or the line above). Only justified "
            "for an app that is genuinely never published through the "
            "marketplace pipeline.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k012"
        ),
    ),
    RuleDefinition(
        id="K013",
        scope=RuleScope.APP,
        name="ManifestNodeAppNameMisattributed",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.18.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "A DAG node's app_name is the identity its logs, metrics and "
            "failures are filed under: the SDK tags log records with it and the "
            "tenant's Workflow Center reads them back by it, so a wrong value "
            "makes the step's logs unreachable rather than merely mislabelled -- "
            "the step shows 'No error logs available for this pod' even though "
            "logging worked. Nothing else catches it, because the node still "
            "runs correctly: task_queue governs dispatch, so a misattributed "
            "node executes in the right place and only its telemetry goes to the "
            "wrong app. That is why the drift survived across the connector "
            "fleet unnoticed (CNCT-24, CNCT-129). Two signals establish the "
            "owning app independently. Automation Engine hosts none of the "
            "toolkit-owned workflows, so QueryIntelligenceWorkflow paired with "
            "app_name 'automation-engine' is impossible rather than merely "
            "suspect -- the signature of a contract that hand-wrote a raw "
            "DAGNode instead of the matching node class and inherited the "
            "default. Separately, a task queue naming a known system app says "
            "which worker polls the node whatever its workflow type. Both are "
            "exact-match checks against closed sets, so neither guesses."
        ),
        short_description=(
            "Generated manifest DAG node declares an app_name that disagrees "
            "with the app its workflow type or task queue says runs it"
        ),
        full_description=(
            "A node in a committed generated ``manifest.json`` declares an "
            "``app_name`` that disagrees with the app actually running it. Two "
            "independent signals are checked, each against a closed set:\n"
            "\n"
            "**1. The workflow type.** Automation Engine hosts none of the "
            "toolkit-owned workflows -- each has its own worker:\n"
            "\n"
            "    QueryIntelligenceWorkflow -> query-intelligence\n"
            "    PublishWorkflow           -> publish\n"
            "    LineageWorkflow           -> lineage\n"
            "    PopularityWorkflow        -> popularity\n"
            "    NotificationWorkflow      -> notification-app\n"
            "\n"
            "One of these paired with ``app_name: automation-engine`` is drift by "
            "construction: the contract hand-wrote a raw ``DAGNode`` instead of "
            "the matching node class and inherited the default. An ``app_name`` "
            "set to some *other* value is left alone -- that may be a bespoke "
            "worker, which is the author's call.\n"
            "\n"
            "**2. The task queue.** A queue of the form ``atlan-<system-app>-...`` "
            "names the worker that polls the node, and so the app that runs it, "
            "whatever its workflow type. A disagreeing ``app_name`` is "
            "misattributed. Only the *app* segment is matched, against the known "
            "system apps -- the suffix (``-production``, "
            "``-{deployment_name}``, a tenant name) is not interpreted.\n"
            "\n"
            "Why nothing else catches it: ``task_queue`` governs dispatch, so a "
            "misattributed node still executes in the right place. Only its "
            "telemetry goes astray -- the logs are written under one identity and "
            "read back under another, so the step shows ``No error logs "
            "available for this pod`` even though logging worked.\n"
            "\n"
            "**Fix:** in ``contract/app.pkl``, either replace the hand-written "
            "``DAGNode`` with the matching built-in node class "
            "(``QueryIntelligenceNode``, ``PublishNode``, ``LineageNode``, "
            "``PopularityNode``, ``NotificationNode``), which sets ``appName`` "
            "and ``taskQueue`` together, or set ``appName`` explicitly to the app "
            "named in the finding. Then regenerate with ``pkl eval -m . "
            "contract/app.pkl``.\n"
            "\n"
            "**Change ``appName`` only.** The rule never asks for a ``taskQueue`` "
            "change: the queue is the routing decision, and in these manifests it "
            "is generally already right. Rewriting it would move where the node "
            "runs.\n"
            "\n"
            "**No suppression is available.** The finding is anchored on a "
            "generated ``.json`` artifact, which has no comment syntax to carry "
            "a directive -- as with K009, the only resolution is to fix the "
            "contract and regenerate. Never hand-edit ``manifest.json``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k013"
        ),
    ),
    RuleDefinition(
        id="K014",
        scope=RuleScope.APP,
        name="ReleaseModelUndeclared",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.18.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "release_model decides whether merging to main publishes the app to "
            "every tenant. The publish step reads it out of the committed "
            "atlan.yaml and defaults a missing key to 'cd' "
            "(.github/scripts/parse_atlan_yaml.py), so an app that never "
            "declares it auto-publishes to channel='all' on every merge -- not "
            "because anyone chose continuous delivery, but because the key was "
            "absent. That default is silent by construction: the app builds, "
            "tests and publishes successfully, so no gate has any reason to "
            "speak up, and the release model an app is actually running is "
            "invisible in review. A fleet sweep found 36 of 75 connectors in "
            "exactly that state, none of them deliberately. This rule does not "
            "prefer either model -- 'cd' is a legitimate choice -- it only "
            "requires that the choice be written down, so it is reviewable and "
            "cannot be inherited by accident."
        ),
        short_description=(
            "atlan.yaml declares no top-level release_model, so the app "
            "silently inherits the 'cd' default and auto-publishes on merge"
        ),
        full_description=(
            "The committed ``atlan.yaml`` has no usable top-level "
            "``release_model:`` key, or declares a value outside the allowed "
            "set.\n"
            "\n"
            "``release_model`` selects how the app reaches tenants:\n"
            "\n"
            "    cd      every merge to main publishes to channel='all'\n"
            "    semver  merges build only; a GitHub Release publishes to all\n"
            "\n"
            "A missing key is read as ``cd`` "
            "(``.github/scripts/parse_atlan_yaml.py``), so omitting it opts the "
            "app into fleet-wide publish-on-merge by default. Nothing else "
            "reports this: the omission breaks no build and fails no test, and "
            "the effective model never appears in a diff.\n"
            "\n"
            "**This rule takes no side between ``cd`` and ``semver``.** It fires "
            "only on an *undeclared* or *invalid* value. Declaring "
            "``release_model: cd`` explicitly satisfies it.\n"
            "\n"
            "``versioned`` is a deprecated alias for ``semver``, normalised on "
            "read; it is reported so it can be migrated.\n"
            "\n"
            "**Fix -- where the key goes depends on whether the contract emits "
            "atlan.yaml.** Determine that by running ``pkl eval -m <out> "
            "contract/app.pkl`` and checking whether ``atlan.yaml`` appears in "
            "the output. Do not infer it from the presence of "
            "``contract/app.pkl`` or from which template the contract "
            "``amends`` -- both are unreliable, and a ``NativeApp.pkl`` contract "
            "can still emit ``atlan.yaml`` through its own "
            "``additionalOutputFiles`` block.\n"
            "\n"
            "*The contract emits it* -- declare it at the source, in the pkl "
            "``metadata`` mapping, whose entries are emitted as top-level "
            "``atlan.yaml`` keys (the same untyped hatch K011 prescribes for "
            "``app_id``), then regenerate:\n"
            "\n"
            "    metadata {\n"
            '      ["release_model"] = "semver"\n'
            "    }\n"
            "\n"
            "If the contract instead builds ``atlan.yaml`` itself via an inline "
            '``additionalOutputFiles["atlan.yaml"]`` mapping, add the key to '
            "that mapping. Either way, never hand-edit a generated "
            "``atlan.yaml`` -- K005 guards its provenance banner and the next "
            "toolkit bump reverts the edit.\n"
            "\n"
            "*The contract does not emit it* -- ``atlan.yaml`` is hand-owned; "
            "add ``release_model:`` to it directly.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K014] <reason>`` on the "
            "first line of ``atlan.yaml`` (or the line above the key). A "
            "suppression is rarely the right answer: declaring the value is one "
            "line and is the entire point of the rule.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k014"
        ),
    ),
    RuleDefinition(
        id="K015",
        scope=RuleScope.APP,
        name="LegacyWorkflowTypeContractDrift",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.23.0",
        orthogonal_gate="tests",
        rationale=(
            "An inbound-only workflow type alias keeps a worker answering a "
            "pre-migration Temporal type that external callers have not stopped "
            "dispatching. Once an app carries a contract tree the alias is "
            "declared twice: in the generated manifest's legacy_workflow_types "
            "block, which is the contracted declaration site, and in the SDK's "
            "App.legacy_workflow_types class attribute, which is what actually "
            "registers with the worker. Neither site validates the other, and a "
            "disagreement is silent in both directions. An alias only in the "
            "manifest is one the contract advertises and the worker rejects: the "
            "unmigrated caller keeps dispatching and keeps failing, which is the "
            "exact outage the alias existed to prevent. An alias only in code is "
            "one P016 no longer credits, so a genuinely routed entry point reads "
            "as drift and the app is blocked on a false finding. Nothing else "
            "notices either shape -- the app builds, the contract generates, and "
            "the mismatch only surfaces as a dispatch failure in production.\n"
            "\n"
            "Customer impact: an alias exists because unmigrated callers are "
            "still dispatching a pre-migration workflow type. When the manifest "
            "advertises an alias the worker never registered, every one of those "
            "callers keeps failing at dispatch -- the crawl simply never starts, "
            "and the contract says it should. That is the precise outage the "
            "alias was added to prevent, reintroduced silently.\n"
            "\n"
            "This blocks rather than warns because P016 -- itself a blocking "
            "rule -- now routes off the manifest block. A drifted block does not "
            "merely go unnoticed; it changes what another blocking rule "
            "concludes, so the two must be held together at the same strength. "
            "The surface is new and no app declares aliases yet, so nothing in "
            "the fleet is blocked by adopting it at this tier."
        ),
        short_description=(
            "the manifest's legacy_workflow_types block and the SDK App's "
            "legacy_workflow_types declaration do not agree"
        ),
        full_description=(
            "The generated ``app/generated/**/manifest.json`` "
            "``legacy_workflow_types`` block and the SDK ``App`` subclass's "
            "``legacy_workflow_types`` class attribute must declare the same "
            "``alias -> entry-point`` pairs and the same expiry.\n"
            "\n"
            "The rule fires on four shapes:\n"
            "\n"
            "* an alias declared in code that the manifest does not carry;\n"
            "* an alias declared in the manifest that the ``App`` does not;\n"
            "* a ``removal_version`` that differs between the two sites;\n"
            "* per-entry-point manifests that disagree with each other (the "
            "block is app-level, so every copy must be identical).\n"
            "\n"
            "A ``legacy_workflow_types`` assignment the scan cannot read "
            "statically -- a variable, a comprehension -- is reported too: the "
            "comparison cannot be made at all, so neither agreement nor drift "
            "can be established.\n"
            "\n"
            "Only the class attribute registers the alias with the worker. Only "
            "the manifest block is read by P016 when it decides whether a bare "
            "DAG node routes an entry point. That split is why drift is "
            "invisible: each site is individually well-formed.\n"
            "\n"
            "**Fix -- declare the same thing twice, on purpose.** In the "
            "contract:\n"
            "\n"
            ".. code-block:: pkl\n"
            "\n"
            "    legacyWorkflowTypes {\n"
            "      new LegacyWorkflowTypeSpec {\n"
            '        alias = "LegacyCrawlerWorkflow"\n'
            '        entrypoint = "crawler"\n'
            "      }\n"
            "    }\n"
            '    legacyWorkflowTypesRemovalVersion = "4.2.0"\n'
            "\n"
            "then regenerate, and in the app:\n"
            "\n"
            ".. code-block:: python\n"
            "\n"
            "    class MyApp(App):\n"
            "        legacy_workflow_types = {\n"
            '            "LegacyCrawlerWorkflow": "crawler",\n'
            "        }\n"
            '        legacy_workflow_types_removal_version = "4.2.0"\n'
            "\n"
            "The block is app-level, so for a multi-entrypoint bundle the **same** "
            "block goes on every entry point's contract and every generated "
            "manifest carries an identical copy. The bundle root renders no "
            "manifest and refuses the declaration at eval time.\n"
            "\n"
            "An app with no ``app/generated/`` tree is out of scope: the class "
            "attribute is then the only declaration site and there is nothing to "
            "compare.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K015] <reason>`` above the "
            "``App`` subclass. Suppressing leaves the two sites free to diverge, "
            "and P016 keeps routing off the manifest either way.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k015"
        ),
    ),
    RuleDefinition(
        id="K016",
        scope=RuleScope.APP,
        name="EntrypointArtifactSchemaMissing",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.23.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "Data crosses app boundaries as files, and at every hand-off the "
            "producer's idea of the artifact's shape and the consumer's idea of "
            "it are independent beliefs that nothing checks. A production RCA "
            "traced 73 days of frozen lineage to one column that had become a "
            "string where the consumer expected a timestamp; every workflow in "
            "the chain reported success throughout, because each one did "
            "exactly what its own code said and no layer compared the two "
            "beliefs. Checksums do not help -- storage integrity attests that "
            "the bytes read are the bytes written and is explicit that this "
            "proves nothing about the artifact being semantically what the "
            "reader expects. artifactSchemas is where the shape gets written "
            "down, and this rule requires it exactly where the hand-off is "
            "public: an entry point's contracts are read by another app or by "
            "the DAG, so an undeclared FileReference there is an interface "
            "nobody can check. Internal @task contracts are deliberately "
            "exempt -- that processing is the app's own, and the app decides "
            "whether it wants the check. The absence of a declaration is a "
            "structural fact about two committed files, not a heuristic, so "
            "this rule cannot produce a false positive; it needs only a "
            "deprecation window, which the SDK's matching registration-time "
            "warning provides."
        ),
        short_description=(
            "An entry point's input/output contract declares a FileReference "
            "field that no artifactSchemas entry describes"
        ),
        full_description=(
            "An entry point's ``input``/``return`` contract declares a "
            "``FileReference`` field -- directly or inherited from a base or "
            "SDK mixin -- and the entry point's committed "
            "``artifact_schemas.json`` carries no entry keyed by that field "
            "name.\n"
            "\n"
            "**Why the entry-point boundary specifically.** An entry point's "
            "contracts are public by definition: another app or the platform "
            "DAG reads them. The default ``run()`` method is registered as an "
            "*implicit* entry point carrying the same metadata as an explicit "
            "``@entrypoint``, so the rule is uniformly every entry point's "
            "``input_type`` and ``output_type`` and needs no special-casing. "
            "Internal ``@task`` contracts never become entry points and are "
            "exempt.\n"
            "\n"
            "**Both directions are checked.** For a cross-app hand-off the "
            "*consumer* declares what it requires of its input, and the "
            "producer references the consumer's published declaration rather "
            "than re-authoring the field list -- so an entry point's Input "
            "carries declarations exactly like its Output does.\n"
            "\n"
            "**Fix -- declare the shape in the contract, then regenerate.** "
            "``artifactSchemas`` is a per-entry-point pkl property, keyed by "
            "the contract field name (never by a storage path):\n"
            "\n"
            "    artifactSchemas {\n"
            '      ["raw_queries"] = new ArtifactSchema {\n'
            '        format = "parquet"   // or "ndjson"\n'
            "        fields {\n"
            "          new ArtifactField {\n"
            '            name = "QUERY_ID"\n'
            '            type = "string"\n'
            '            description = "Warehouse-assigned query id; the join key."\n'
            "          }\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "\n"
            "Then ``pkl eval -m . contract/app.pkl``. A single-entry-point app "
            "emits ``app/generated/artifact_schemas.json``; a multi-entry-point "
            "bundle emits ``app/generated/{entrypoint}/artifact_schemas.json`` "
            "per entry point. Declaring ``artifactSchemas`` on a bundle *root* "
            "is a generation error -- the root has no contract model, so a key "
            "there could not name a real field.\n"
            "\n"
            "Never hand-edit the generated ``artifact_schemas.json``: it is a "
            "pkl eval output and the next toolkit run reverts the edit.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K016] <reason>`` on the "
            "field declaration, or on the contract class definition for a field "
            "inherited from a base. Suppressing states that this hand-off is "
            "deliberately unchecked -- which is a defensible call for an "
            "artifact no other app reads, and the wrong call for one that "
            "crosses an app boundary.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k016"
        ),
    ),
    RuleDefinition(
        id="K017",
        scope=RuleScope.APP,
        name="ArtifactSchemaWriterMismatch",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-toolkit",
        autofixable=False,
        since="0.23.0",
        orthogonal_gate="pkl-eval",
        rationale=(
            "K016 requires a declaration where a hand-off is public. This rule "
            "is the next failure along: the declaration exists, and the app's "
            "own Python contradicts it. That is worse than no declaration at "
            "all -- an absent one is visibly absent, while a stale one reads as "
            "a true statement about the file, so a consuming app trusts it and "
            "builds on it. The same production RCA that motivated ADR-0020 "
            "traced 73 days of frozen lineage to a column whose real type had "
            "drifted from what the reader expected; every workflow in the chain "
            "reported success throughout, because each did exactly what its own "
            "code said and no layer compared the two beliefs. A writer moving "
            "on without its declaration is exactly how that gap opens. The SDK "
            "finds the same disagreement at runtime, but only once a run has "
            "produced the artifact and only in the environment that ran it; "
            "this rule finds it in review, before merge, where it costs "
            "nothing.\n"
            "\n"
            "It warns rather than blocks because it is inference about code "
            "rather than a structural fact about two committed files: the check "
            "resolves the writer's path and record type through local "
            "assignments, and although every unresolvable shape is dropped "
            "rather than guessed at, a WARN tier is the honest disposition for "
            "a rule whose evidence is a read of Python rather than a diff of "
            "two artifacts. Nothing in the fleet declares artifact schemas yet, "
            "so adopting it blocks no app either way."
        ),
        short_description=(
            "A declared artifact schema disagrees with the Python that writes "
            "the artifact"
        ),
        full_description=(
            "An ``artifactSchemas`` entry in the committed "
            "``artifact_schemas.json`` contradicts the app's own writer for the "
            "same ``FileReference`` contract field.\n"
            "\n"
            "Two disagreements are reported:\n"
            "\n"
            "* **Format.** The writer builds the field's ``FileReference`` from "
            "a path whose extension the declared ``format`` cannot be -- a "
            "``.parquet`` path declared ``ndjson``, or a ``.jsonl``/``.ndjson``/"
            "``.json`` path declared ``parquet``. Any other extension, and a "
            "reference to a directory, is skipped: a partitioned-parquet "
            "directory has no extension to disagree with.\n"
            "* **Fields.** The record class the writer serialises into the "
            "artifact declares a field -- directly or inherited -- that the "
            "declaration does not describe. Declared nested paths "
            "(``attributes.columns[].name``) are compared at their top-level "
            "segment, since that is the level a record class exposes.\n"
            "\n"
            "**What the rule will not do.** Resolution is module-scoped and "
            "deliberately narrow. A path variable is followed only within the "
            "file it is assigned in; a name assigned two different extensions, "
            "a handle opened from two different paths, and a write whose "
            "argument names more than one candidate record class are each "
            "recorded as unknown rather than resolved by choosing. Only classes "
            "defined in the scanned repo count as record types, so a writer "
            "that serialises through a mapper or a library model is invisible "
            "to the field half of the rule. A record class that renames fields "
            "on the wire (``rename=``, ``Field(alias=...)``, "
            "``alias_generator=``) is skipped entirely, because its attribute "
            "names are not the artifact's field names. Every one of those is a "
            "deliberate false negative.\n"
            "\n"
            "**Fix -- change whichever side is wrong.** If the declaration is "
            "right, correct the writer. If the writer is right, edit the pkl "
            "contract and regenerate:\n"
            "\n"
            "    artifactSchemas {\n"
            '      ["transformed_entities"] = new ArtifactSchema {\n'
            '        format = "ndjson"\n'
            "        fields {\n"
            "          new ArtifactField {\n"
            '            name = "typeName"\n'
            '            type = "string"\n'
            '            description = "Atlan type this record instantiates."\n'
            "          }\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "\n"
            "Then ``pkl eval -m . contract/app.pkl``. Never hand-edit the "
            "generated ``artifact_schemas.json``: it is a pkl eval output and "
            "the next toolkit run reverts the edit.\n"
            "\n"
            "**Suppress** with ``# conformance: ignore[K017] <reason>`` on the "
            "``FileReference`` construction. Suppressing states that the "
            "declaration and the writer are allowed to disagree, which leaves "
            "the consuming side reading an assertion the producer does not "
            "honour.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/contract-toolkit.md#k017"
        ),
    ),
)

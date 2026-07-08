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
are WARN and APP-scoped, and no-op on any repo without a ``contract/`` directory.

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
            "explicit imports of Config.pkl/Connectors.pkl/Credential.pkl/"
            "Renderers.pkl (App.pkl re-exports what apps need as typealiases).  "
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
            '* ``import "…Connectors.pkl"`` — App.pkl exposes connector '
            "constants as ``Connectors.*`` with no import needed; remove the "
            "explicit import.\n"
            "\n"
            '* ``import "…Credential.pkl"`` and ``import "…Renderers.pkl"`` '
            "(Argo-era modules) — no longer used by App.pkl; remove both.\n"
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
        tier=EnforcementTier.WARN,
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
            "longer claims.  Renovate's renovate-pkl-sync workflow keeps these two in "
            "sync on bot bumps, but a manual pin edit bypasses it entirely.  Comparing "
            "the two files is a pure, deterministic text check that needs no pkl "
            "toolchain, so it catches the drift the moment it lands (BLDX-1414)."
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
            "On a ``renovate/**`` branch this happens automatically via the "
            "``renovate-pkl-sync`` workflow; on a manual bump run the commands "
            "above (or ``uv run poe generate`` where the app defines it).\n"
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
)

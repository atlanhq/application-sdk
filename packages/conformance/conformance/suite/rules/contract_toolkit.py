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
            "Never hand-edit generated artifacts — C002 catches staleness.\n"
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
            "``atlan.yaml``.  Never hand-edit generated artifacts — C002 "
            "catches staleness.\n"
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
)

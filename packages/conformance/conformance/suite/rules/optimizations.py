"""Optimisation / recommendation rule definitions (O-series).

Below-the-prescription-bar recommendations and optimisations: things worth
nudging toward (performance, canonical-dependency choices) but not mandatory
enough to block a merge.  O-series rules are ``WARN`` by default; a rule that
earns mandatory status graduates into a category series or the P-series.
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
        id="O001",
        canonical_reference=(
            "atlan-hello-world-app app/connector.py — JSONL is written and read with "
            "`orjson.dumps` / `orjson.loads`. orjson is a core SDK dependency, so there is "
            "no install cost to paying for the speed."
        ),
        scope=RuleScope.BOTH,
        name="OrjsonOverStdlibJson",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="canonical-dependency",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "orjson is already a core SDK dependency — zero incremental cost — and on hot "
            "paths the ~10x throughput advantage compounds at fleet scale. WARN (not block) "
            "because orjson returns bytes not str and has a different option API, so each "
            "site needs human judgment before migrating."
        ),
        short_description="json.dumps()/json.loads() — prefer orjson (a core SDK dependency, ~10x faster)",
        full_description=(
            "``orjson`` is already a core dependency of the application SDK, so it is\n"
            "available to every app, and it is generally *at least* 10x faster than the\n"
            "stdlib ``json`` module.  Prefer ``orjson.dumps`` / ``orjson.loads`` for\n"
            "serialisation on any hot path.\n"
            "\n"
            "Only ``json.dumps`` and ``json.loads`` call sites are flagged, and only\n"
            "when ``json`` resolves to the stdlib module (an ``import json`` /\n"
            "``import json as …`` / ``from json import dumps|loads`` binding).  Bare\n"
            "``.json()`` attribute calls (e.g. ``response.json()``) are never flagged.\n"
            "``json.JSONDecodeError`` handling, ``json.dump``/``json.load`` (file-object\n"
            "APIs orjson does not provide), and custom ``JSONEncoder`` subclasses are\n"
            "out of scope.\n"
            "\n"
            "NOT autofixable: ``orjson`` is not a drop-in replacement.  ``orjson.dumps``\n"
            "returns ``bytes`` (not ``str``), has no ``indent=`` / ``sort_keys=`` /\n"
            "``default=`` keyword surface (use ``option=orjson.OPT_INDENT_2 |\n"
            "orjson.OPT_SORT_KEYS`` and the ``default`` positional), and rejects some\n"
            "inputs stdlib accepts.  A blind ``json.``→``orjson.`` swap silently changes\n"
            "``str``→``bytes`` and breaks callers — each site needs human judgement.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o001",
    ),
    RuleDefinition(
        id="O002",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — assets are serialised through "
            "`asset.to_nested_bytes()`, the v9 wire shape, rather than through `.dict()`."
        ),
        scope=RuleScope.APP,
        name="LegacyAssetSerialization",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "The asset-mapper pattern serialises pyatlan assets to JSONL with the v9 "
            "API — asset.to_nested_bytes() — which emits the nested-entity wire shape "
            "the platform expects. Serialising an asset with the pydantic .dict() "
            "method produces a flat dict that still needs hand-conversion and drifts "
            "from the SDK's recommended pipeline (BLDX-1492; docs/upgrade-guide-v3.md). "
            "WARN/recommendation because .dict() is name-anchored — it can also belong "
            "to a non-asset pydantic model — so the call needs a human glance."
        ),
        short_description=(
            "Asset serialised with .dict() — prefer the v9 asset.to_nested_bytes() API"
        ),
        full_description=(
            "Flags a ``.dict()`` method call in a module that imports pyatlan asset\n"
            "models.  The asset-mapper pattern writes assets with the v9 serialisation\n"
            "API — ``asset.to_nested_bytes()`` — not the pydantic ``.dict()`` form\n"
            "(``docs/upgrade-guide-v3.md`` explicitly says 'use the v9 serialisation\n"
            "API instead of .dict()').\n"
            "\n"
            "Coverage limits (biased to low false-positives at WARN): only ``.dict()``\n"
            "is matched (not ``.json()``, which is overwhelmingly ``response.json()``\n"
            "on HTTP clients), and only in files that import asset models.  A\n"
            "``.dict()`` on a *non-asset* pydantic model in such a file is a known\n"
            "false-positive — suppress with ``# conformance: ignore[O002] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o002",
    ),
    RuleDefinition(
        id="O003",
        canonical_reference=(
            "atlan-openapi-app app/asset_mapper.py — `map_connection` is annotated `-> "
            "Connection`, the pyatlan type it actually builds, so a wrong asset type is a "
            "type error rather than a runtime surprise in the payload."
        ),
        scope=RuleScope.APP,
        name="UntypedAssetMapperReturn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "The asset-mapper pattern's value is end-to-end typing: a mapper function "
            "constructs a pyatlan asset and returns it, so the return annotation "
            "documents which asset it produces and lets pyright check the call site. "
            "A mapper that builds an asset but declares no return type loses that "
            "guarantee (BLDX-1492; reference app atlan-openapi-app). WARN/recommendation "
            "because adding the annotation is a safe, mechanical nudge."
        ),
        short_description=(
            "Function builds a pyatlan asset but has no return annotation — annotate "
            "it with the asset type"
        ),
        full_description=(
            "Flags a function that constructs a pyatlan asset (instantiates a class\n"
            "imported from ``pyatlan_v9.model.assets`` / ``pyatlan.model.assets``) and\n"
            "**returns that asset**, but carries no ``-> <Asset>`` return annotation.\n"
            "The asset-mapper pattern is typed end-to-end — each ``map_<entity>``\n"
            "function declares the pyatlan asset it produces (see ``atlan-openapi-app``).\n"
            "\n"
            "Keyed on actually returning the constructed asset (``return Table(...)`` or\n"
            "``asset = Table(...); ... return asset``), not just a ``map_`` name — so a\n"
            "helper that builds an asset as a side effect and returns something else\n"
            "(e.g. ``return record.id``) is not flagged, and the suggested annotation\n"
            "always matches the real return.  Suppress with\n"
            "``# conformance: ignore[O003] <reason>`` when an untyped return is\n"
            "intentional.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o003",
    ),
    RuleDefinition(
        id="O004",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — `from pyatlan_v9.model.assets import Column, "
            "Database, Procedure, Schema, Table, View`. The non-v9 pyatlan.model.assets "
            "path appears in none of the four reference apps."
        ),
        scope=RuleScope.APP,
        name="LegacyPyatlanAssetImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "pyatlan_v9 is the SDK's go-forward asset surface for the asset-mapper "
            "pattern; the legacy pyatlan.model.assets classes are the memory-heavy "
            "DataFrame/transformer-era serialization path, kept only for connectors "
            "still on the built-in AtlasTransformer (which B001 steers off). pyatlan_v9 "
            "ships inside the existing pyatlan>=9 dependency, so the switch adds nothing "
            "to resolve. A below-the-bar recommendation (O-series, WARN): the v9 models "
            "differ in attributes and serialization (to_nested_bytes vs .dict()), so "
            "each site needs human judgement — never a blind name swap."
        ),
        short_description=(
            "Imports pyatlan.model.assets (non-v9) — prefer pyatlan_v9.model.assets"
        ),
        full_description=(
            "Flags app code that imports asset model classes from the legacy\n"
            "``pyatlan.model.assets`` package, in any of the three import forms:\n"
            "``from pyatlan.model.assets import X``, ``import pyatlan.model.assets``,\n"
            "or ``from pyatlan.model import assets``.  Detection is import-anchored\n"
            "(an asset class is only imported in order to construct it), so the rare\n"
            "fully-qualified ``pyatlan.model.assets.X(...)`` form with no matching\n"
            "import is out of scope.  New connectors should build assets from\n"
            "``pyatlan_v9.model.assets`` — the optimized v9 surface the asset-mapper\n"
            "pattern is built on (BLDX-1492; see\n"
            "``docs/guides/sql-application-guide.md`` and ``docs/upgrade-guide-v3.md``).\n"
            "\n"
            "Scope is deliberately narrow — only ``pyatlan.model.assets`` is matched,\n"
            "never the rest of ``pyatlan``: enums and helpers that legitimately have\n"
            "no v9 equivalent (e.g. ``from pyatlan.model.enums import\n"
            "AtlanConnectorType``) are out of scope.\n"
            "\n"
            "NOT autofixable: the v9 models are not a drop-in rename — attribute\n"
            "names and the serialization API differ (use ``asset.to_nested_bytes()``\n"
            "rather than ``.dict()``), so each construction site needs review.\n"
            "Suppress with ``# conformance: ignore[O004] <reason>`` when a connector\n"
            "is intentionally pinned to the legacy ``AtlasTransformer`` surface.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o004",
    ),
    RuleDefinition(
        id="O006",
        canonical_reference=(
            "No reference app imports rocksdict. The SDK seam is "
            "application_sdk/common/spillable_dict.py — `SpillableDict`, which pickles "
            "values so a caller needs no hand-rolled serialize/deserialize step around the "
            "store."
        ),
        scope=RuleScope.APP,
        name="DirectRocksdictImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="canonical-dependency",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "SpillableDict (application_sdk.common.spillable_dict) already wraps "
            "rocksdict.Rdict as a MutableMapping and pickles values directly, so it "
            "carries none of the hand-rolled-serialization risk a from-scratch "
            "wrapper does. Two connectors (atlan-thoughtspot-app, "
            "atlan-aws-smus-app) independently hand-rolled the same RocksDB-backed "
            "DiskLookup with an asymmetric JSON serialize/deserialize step — put() "
            "special-cased str, get() unconditionally ran json.loads() — so a "
            "stored string that was also valid bare JSON (a numeric-looking name, "
            "'true', 'null') silently came back as int/bool/None instead of str "
            "(CNCT-80, CNCT-191). WARN (not block) because a from-scratch wrapper "
            "may have a deliberate reason (custom RocksDB Options, a key type "
            "outside str/int/float/bool/bytes) that needs a human glance before "
            "migrating."
        ),
        short_description=(
            "Imports rocksdict directly — prefer the SDK's SpillableDict "
            "(pickles values, no hand-rolled serialize/deserialize step)"
        ),
        full_description=(
            "Flags app code that imports the ``rocksdict`` package directly, in "
            "either import form: ``from rocksdict import Rdict`` or ``import "
            "rocksdict``.  Detection is import-anchored (a direct ``rocksdict`` "
            "import is the unambiguous signal — nothing else pulls that dependency "
            "in).\n"
            "\n"
            "The SDK ships ``application_sdk.common.spillable_dict.SpillableDict`` "
            "— a ``MutableMapping``-compatible, disk-backed dict built on the same "
            "``rocksdict.Rdict``, which pickles values directly rather than "
            "hand-rolling a serialize/deserialize step.  It exists specifically so "
            "connector apps stop reinventing this wrapper.\n"
            "\n"
            "Motivating incident: ``atlan-thoughtspot-app`` and "
            "``atlan-aws-smus-app`` each independently wrote a ``DiskLookup`` class "
            "directly on ``rocksdict.Rdict`` with the identical bug — a value's "
            "``str`` type was silently lost on a round-trip through JSON when the "
            "string happened to also be valid bare JSON.  Neither connector's "
            "hand-rolled wrapper was calling anything the SDK had a fleet-wide "
            "signal for at the time; this rule is that signal going forward.\n"
            "\n"
            "NOT autofixable: ``SpillableDict``'s key type is restricted to "
            "``str | int | float | bool | bytes`` and it has no equivalent to a "
            "custom ``rocksdict.Options`` tuning surface, so each call site needs "
            "review before migrating.  Suppress with ``# conformance: "
            "ignore[O006] <reason>`` when a from-scratch wrapper is deliberate "
            "(e.g. custom RocksDB tuning, or association-list output like "
            "``rocks_backed_dict.py``'s ``append_to_key`` that ``SpillableDict`` "
            "does not provide).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o006",
    ),
    RuleDefinition(
        id="O005",
        canonical_reference=(
            "atlan-hello-world-app app/connector.py — the App declares `name = "
            '"hello-world"` and atlan.yaml carries the same literal. The name is '
            "resolved once, at declaration; a `{app_name}` left in a plain string is a "
            "substitution nothing will ever perform."
        ),
        scope=RuleScope.BOTH,
        name="UnresolvedAppNamePlaceholder",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dag-write-path",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "A plain string literal carrying an unsubstituted '{app_name}' token "
            "freezes the literal token into whatever it's assigned to instead of the "
            "real app name — the exact shape that shipped a task queue no worker "
            "polls, hanging dbt:process to its 24h heartbeat backstop (CONNECT-183). "
            "The substitution was independently hand-rolled at least four times "
            "(Heracles, native-migration-app, atlan-local-marketplace-app "
            "CONNECT-191/#539, atlan-hightouch-app ARUN-1039), and one of those "
            "shipped a double prefix (DISTR-834). A canonical helper — "
            "application_sdk.common.task_queue (derive_task_queue, "
            "resolve_manifest_tokens) — lands in the SDK release that ships "
            "FND-195, so remediation then has a single target; this rule still "
            "checks the unresolved shape rather than a missing "
            "import, because the writers most worth catching are hand-authored "
            "templates that import nothing. WARN (not BLOCK): a template legitimately "
            "resolved by a caller in a different file cannot be seen from here, so "
            "each hit needs a human glance rather than an automatic fail."
        ),
        short_description=(
            "Hardcoded '{app_name}' left unsubstituted in a plain string literal"
        ),
        full_description=(
            "Flags a string ``ast.Constant`` containing the literal substring\n"
            "``{app_name}`` when the token can actually **reach a value** — including\n"
            'the pieces of an escaped-brace f-string: ``f"atlan-{{app_name}}-prod"``\n'
            "is *not* interpolated, its runtime value is the literal\n"
            "``atlan-{app_name}-prod``, so it freezes the token exactly like a plain\n"
            "literal. A token that only ever appears in prose or in a diagnostic\n"
            "cannot freeze into an identifier, and flagging it just teaches people\n"
            "to suppress the rule.\n"
            "\n"
            "Not flagged:\n"
            "\n"
            '* part of a *resolving* f-string (``f"...{app_name}..."`` interpolates\n'
            "  at parse time and no ``{app_name}`` text survives into its pieces),\n"
            "* the receiver of a ``.format(...)`` call whose keywords include\n"
            "  ``app_name=`` (a proper, already-resolving substitution site),\n"
            "* **documentation** — the value of any bare string expression statement.\n"
            "  This covers module/class/function docstrings *and* PEP 257 attribute\n"
            "  docstrings, which are not the first statement of their class body,\n"
            "* **diagnostic text** — inside the arguments of a logging call,\n"
            "  ``warnings.warn(...)``, or a ``raise``; reporting on the token requires\n"
            "  quoting it,\n"
            "* **a token sentinel or message constant** — bound to an ``ALL_CAPS``\n"
            "  name where the literal is exactly the token (the token's own\n"
            "  definition, e.g. ``APP_NAME_TOKEN``) or the name ends in a prose\n"
            "  segment (``_MESSAGE``, ``START_MESSAGE``, ``VALIDATION_RATIONALE``).\n"
            "\n"
            "The exclusions stay narrow: an ``ALL_CAPS`` name holding a real queue\n"
            'template (``TASK_QUEUE = "atlan-{app_name}-prod"``) is still flagged —\n'
            "and so is one whose name merely *contains* a prose fragment without a\n"
            "trailing boundary (``MESSAGE_QUEUE``, ``HELP_QUEUE``) — as are keyword\n"
            "arguments, values at any depth in a DAG literal, and returned templates.\n"
            "\n"
            "Detection is shape-anchored rather than import-anchored, because the\n"
            "writers most worth catching are hand-authored templates outside the SDK\n"
            "that import nothing at all. Remediation: an f-string or\n"
            "``.format(app_name=...)`` when the name is in scope; the shared\n"
            "``application_sdk.common.task_queue`` helper (``derive_task_queue`` /\n"
            "``resolve_manifest_tokens``) lands in the SDK release that ships\n"
            "FND-195 and is the canonical target once available.\n"
            "\n"
            "NOT autofixable: the correct fix depends on where ``app_name`` is\n"
            "actually available in scope — sometimes an f-string is right, sometimes\n"
            "the value needs threading in from a caller first. Suppress with\n"
            "``# conformance: ignore[O005] <reason>`` for a template resolved by a\n"
            "caller in a different file than the one being scanned.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o005",
    ),
)

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
        scope=RuleScope.APP,
        name="LegacyAssetSerialization",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
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
        scope=RuleScope.APP,
        name="UntypedAssetMapperReturn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
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
            "returns a value, but carries no ``-> <Asset>`` return annotation.  The\n"
            "asset-mapper pattern is typed end-to-end — each ``map_<entity>`` function\n"
            "declares the pyatlan asset it produces (see ``atlan-openapi-app``).\n"
            "\n"
            "Keyed on actual asset construction (not just a ``map_`` name), so plain\n"
            "helpers are not flagged.  Suppress with\n"
            "``# conformance: ignore[O003] <reason>`` when an untyped return is\n"
            "intentional.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o003",
    ),
)

"""Optimisation / recommendation rule definitions (O-series).

Below-the-prescription-bar recommendations and optimisations: things worth
nudging toward (performance, canonical-dependency choices) but not mandatory
enough to block a merge.  O-series rules are ``WARN`` by default; a rule that
earns mandatory status graduates into a category series or the P-series.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="O001",
        name="StdlibJsonOverOrjson",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="canonical-dependency",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.2.0",
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
)

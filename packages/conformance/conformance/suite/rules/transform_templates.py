"""Transform-template rule definitions (P040).

The SDK query transformer (``application_sdk.transformers.query``) compiles an
app's YAML transform templates into a DuckDB ``SELECT`` — each template column
renders as ``{source_query} AS {name}`` — and quotes an identifier only when it
contains a dot.  A bare DuckDB **reserved keyword** used as an identifier
(``column``, ``order``, ``group``, …) therefore reaches DuckDB unquoted and
fails at runtime with a ``ParserException`` on SDK >= 3.22, where the daft-less
runtime routes every transform through DuckDB.  The failure is latent: template
loading, imports, and mocked unit tests all pass; only a real transform run
parses the SQL (observed live on main for a document-store connector in fleet
testing).

Per the P-series stability policy (see ``prescriptions.py``) a P-id is a
permanent public contract and is never renumbered or reused.
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
        id="P040",
        scope=RuleScope.APP,
        name="TransformTemplateReservedKeyword",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="transform-templates",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "The SDK query transformer renders each template column as "
            "'{source_query} AS {name}' and quotes identifiers only when they "
            "contain a dot, so a bare DuckDB reserved keyword ('column', 'order', "
            "'group', ...) used as a template identifier reaches DuckDB unquoted "
            "and every transform of that type fails at runtime with a "
            "ParserException on SDK >= 3.22 (the daft-less runtime executes all "
            "transforms through DuckDB). The breakage is latent — templates load, "
            "imports succeed, mocked tests pass — and surfaced live on main for a "
            "document-store connector in fleet testing only when the full pipeline "
            "ran. Linting the templates catches it at review time."
        ),
        short_description=(
            "Transform SQL template uses an unquoted DuckDB reserved keyword as "
            "an identifier"
        ),
        full_description=(
            "In an app's transform templates (YAML files with a ``columns:`` list\n"
            "of ``name`` / ``source_query`` entries, consumed by\n"
            "``application_sdk.transformers.query``), every identifier that is a\n"
            "DuckDB **reserved keyword** must be SQL-quoted.\n"
            "\n"
            "The transformer compiles each column to ``{source_query} AS {name}``\n"
            "and only auto-quotes identifiers containing a dot.  A bare reserved\n"
            "keyword — ``column``, ``order``, ``group``, ``select``, ``table``,\n"
            "``default``, … — therefore lands in the generated ``SELECT``\n"
            "unquoted, and DuckDB raises ``ParserException`` at runtime for every\n"
            "row batch of that entity type.  On SDK >= 3.22 there is no daft\n"
            "fallback: the DuckDB path is the only transform path.\n"
            "\n"
            'Note that YAML-level quoting does not help: ``name: "column"`` and\n'
            "``name: column`` parse to the same string.  The quotes must be part\n"
            "of the *value* so they survive into the SQL::\n"
            "\n"
            "    columns:\n"
            "      - name: '\"column\"'\n"
            "        source_query: '\"column\"'\n"
            "\n"
            "Identifiers containing a dot (``attributes.order``) are exempt — the\n"
            "SDK quotes those itself.  YAML scalar literals (``true`` / ``false``\n"
            "/ ``null``) are not identifiers and are not flagged.\n"
            "\n"
            "This is a WARN (new-rule tier policy) and the scan is text-based\n"
            "(dependency-free), keyed on files that carry both a ``columns:`` key\n"
            "and a ``source_query:`` key — ordinary CI/Helm YAML never matches.\n"
            "Review before suppressing: on a daft-less SDK the finding is a\n"
            "guaranteed runtime parse failure, not a style preference.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p040"
        ),
    ),
)

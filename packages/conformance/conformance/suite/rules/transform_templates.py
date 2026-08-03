"""Transform-template rule definitions (P040).

The SDK query transformer (``application_sdk.transformers.query``) compiles an
app's YAML transform templates into a DuckDB ``SELECT`` — each flattened column
renders as ``{source_query} AS {name}``.  A bare DuckDB **reserved keyword** in
the ``source_query:`` value (``column``, ``order``, ``group``, ``qualify``, …)
is a *reference* to a source column, reaches DuckDB unquoted, and fails at
runtime with a ``ParserException`` on SDK >= 3.22, where the daft-less runtime
routes every transform through DuckDB.  The failure is latent: template
loading, imports, and mocked unit tests all pass; only a real transform run
parses the SQL (observed live on main for a document-store connector in fleet
testing).

The column *identifier* is deliberately not graded: it lands in the ``AS``
alias slot, which DuckDB does not restrict (``SELECT 1 AS column`` parses), and
in the nested-mapping shape every shipped template uses it is rendered dotted
by ``flatten_yaml_columns`` and therefore quoted by ``quote_column_name``.

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
            "The SDK query transformer renders each flattened column as "
            "'{source_query} AS {name}', so a bare DuckDB reserved keyword "
            "('column', 'order', 'group', 'qualify', ...) used as a source_query "
            "column reference reaches DuckDB unquoted and every transform of that "
            "type fails at runtime with a ParserException on SDK >= 3.22 (the "
            "daft-less runtime executes all transforms through DuckDB). The "
            "breakage is latent — templates load, imports succeed, mocked tests "
            "pass — and surfaced live on main for a document-store connector in "
            "fleet testing only when the full pipeline ran. Linting the templates "
            "catches it at review time. The alias (identifier) position is not "
            "graded: DuckDB accepts a reserved keyword after AS, and the shape "
            "real templates use renders it dotted and therefore quoted."
        ),
        short_description=(
            "Transform SQL template references an unquoted DuckDB reserved "
            "keyword in source_query"
        ),
        full_description=(
            "In an app's transform templates (YAML consumed by\n"
            "``application_sdk.transformers.query``), a ``source_query:`` value\n"
            "that is a bare DuckDB **reserved keyword** must be SQL-quoted.\n"
            "\n"
            "The transformer compiles each flattened column to\n"
            "``{source_query} AS {name}``.  The ``source_query`` lands in the\n"
            "expression slot, where a bare reserved keyword — ``column``,\n"
            "``order``, ``group``, ``select``, ``qualify``, ``pivot``, … — is a\n"
            "column *reference* and DuckDB raises ``ParserException`` at runtime\n"
            "for every row batch of that entity type.  On SDK >= 3.22 there is no\n"
            "daft fallback: the DuckDB path is the only transform path.\n"
            "\n"
            "Real templates express the identifier as a nested YAML mapping key::\n"
            "\n"
            "    columns:\n"
            "      attributes:\n"
            "        someAttr:\n"
            "          source_query: '\"order\"'\n"
            "\n"
            'YAML-level quoting does not help: ``source_query: "order"`` and\n'
            "``source_query: order`` parse to the same string.  The quotes must be\n"
            "part of the *value* so they survive into the SQL.\n"
            "\n"
            "**Scope — the expression position only.**  The column identifier is\n"
            "not graded.  It reaches the ``AS`` alias slot, which DuckDB does not\n"
            "restrict (``SELECT 1 AS column`` and ``AS qualify`` both parse on the\n"
            "pinned 1.5.5), and in the shape that ships ``flatten_yaml_columns``\n"
            "renders it as ``attributes.<key>`` — dotted, hence quoted by\n"
            "``quote_column_name``.  Grading it would describe a runtime failure\n"
            "that does not occur.\n"
            "\n"
            'Values containing a dot, embedded ``"`` quotes, whitespace or ``(``\n'
            "are exempt (auto-quoted, already quoted, or an expression rather than\n"
            "a bare reference).  YAML scalar literals (``true`` / ``false`` /\n"
            "``null``) take the transformer's literal path and are not flagged.\n"
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

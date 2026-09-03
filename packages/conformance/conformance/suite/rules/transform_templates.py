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
alias slot, which DuckDB does not restrict (``SELECT 1 AS column`` and
``AS qualify`` both parse on the pinned 1.5.5).

SDK 3.28.0 fixes this at the root — the transformer quotes a ``source_query``
that resolved as a plain column reference — so P040 is scoped to apps pinned
below it (``superseded_by: sdk>=3.28.0``) rather than dropped: an app on an
older SDK still fails at runtime, and that population is the only one with no
other signal.

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
        canonical_reference=(
            "atlan-mysql-app app/sql/ — ten extraction templates, none of which references "
            "a bare DuckDB reserved keyword as an identifier. Where the source's own "
            "column name collides, quote it in the template; the failure otherwise appears "
            "only at transform time, on the customer's data."
        ),
        scope=RuleScope.APP,
        name="TransformTemplateReservedKeyword",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="transform-templates",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        superseded_by="sdk>=3.28.0",
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
            "graded: DuckDB accepts a reserved keyword after AS, so there is no "
            "runtime failure to report there."
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
            "pinned 1.5.5), so grading it would describe a runtime failure that\n"
            "does not occur.\n"
            "\n"
            'Values containing a dot, embedded ``"`` quotes, whitespace or ``(``\n'
            "are exempt (auto-quoted, already quoted, or an expression rather than\n"
            "a bare reference).  YAML scalar literals (``true`` / ``false`` /\n"
            "``null``) take the transformer's literal path and are not flagged.\n"
            "\n"
            "This is a WARN (new-rule tier policy) and the scan is text-based\n"
            "(dependency-free), keyed on files that carry both a ``columns:`` key\n"
            "and a ``source_query:`` key — ordinary CI/Helm YAML never matches.\n"
            "Review before suppressing: on a daft-less SDK below the fixed\n"
            "version the finding is a guaranteed runtime parse failure, not a\n"
            "style preference.\n"
            "\n"
            "**Version scope — fixed at the root from SDK 3.28.0.**  The\n"
            "transformer now quotes a ``source_query`` that resolved as a plain\n"
            "column reference, so a reserved keyword renders as valid SQL with no\n"
            "template change at all; the ``source_columns``-driven route, which\n"
            "carries arbitrary SQL, is left unquoted.  This rule therefore\n"
            "describes only apps pinned **below** that version and is marked\n"
            "``superseded_by: sdk>=3.28.0`` rather than dropped — an app on an\n"
            "older SDK still fails at runtime, and dropping the rule would take\n"
            "the only static signal away from exactly that population.\n"
            "\n"
            "The marker names the next SDK *minor* rather than the exact patch:\n"
            "the patch number is assigned by release CI at merge time, and\n"
            "erring late only keeps the rule firing on some already-fixed apps,\n"
            "never the reverse.  Retire the rule (set ``until``) once the fleet\n"
            "floor has crossed it.\n"
            "\n"
            "**Do not hand-remediate templates that the version bump fixes.**\n"
            "Embedding quotes in the value was the interim advice and it is worse\n"
            "than it looks on an unfixed SDK: below 3.28.0 the transformer\n"
            "matched ``'\"order\"'`` against the available columns as raw text,\n"
            "found nothing, and dropped the attribute from published output — a\n"
            "silent missing attribute in place of a loud ``ParserException``.\n"
            "From 3.28.0 both spellings resolve and render identically, so the\n"
            "upgrade is the fix and the template edit is unnecessary.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p040"
        ),
    ),
)

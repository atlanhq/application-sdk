"""M002 InternalJargonInDescription — model-driven check (BLDX-1575).

Customer-facing description fields (``short_description``, ``long_description``,
entrypoint ``description``) render in the marketplace, in demos, and in customer
tenants.  Internal jargon there leaks how Atlan talks about itself internally —
the incident that motivated this rule was a connector description that carried
internal wording straight into customer-visible surfaces.

Whether a phrase is "internal jargon" is a judgement, not a regex, so a language
model makes the call; this module only shapes the prompt and maps the verdict to
a :class:`Finding`.
"""

from __future__ import annotations

from conformance.suite.checks._ast_common import _IgnoreDirective
from conformance.suite.checks._model_common import (
    Finding,
    ModelClient,
    TextVerdict,
    make_model_finding,
)

from ._surfaces import Surface

RULE_ID = "M002"

_SYSTEM = (
    "You review customer-facing copy for a data-tooling marketplace. You are "
    "given a single description field that ships to customers (marketplace "
    "listing, in-product, demos). Flag it ONLY if it contains language that is "
    "inappropriate for customers: internal jargon or codenames, internal team "
    "or project names, ticket/issue identifiers, internal URLs or tooling "
    "references, developer TODO/placeholder text, or wording that only makes "
    "sense to the engineers who built it. Ordinary product and technical "
    "vocabulary (database, schema, extraction, lineage, connector, etc.) is "
    "fine and must NOT be flagged. When you flag, copy the exact offending span "
    "into 'evidence' and give a one-sentence, customer-neutral reason in "
    "'explanation'. When in doubt, do not flag."
)


def _user_prompt(surface: Surface) -> str:
    return f"Field: {surface.field} ({surface.location})\n" f"Text:\n{surface.value}"


def scan_surfaces(
    surfaces: list[Surface],
    rel: str,
    directives: dict[int, _IgnoreDirective],
    client: ModelClient,
) -> list[Finding]:
    """Classify each description surface and emit findings for flagged copy."""
    findings: list[Finding] = []
    for surface in surfaces:
        if not surface.is_description:
            continue
        verdict = client.classify(
            system=_SYSTEM, user=_user_prompt(surface), schema=TextVerdict
        )
        if not verdict.flagged:
            continue
        message = (
            f"Customer-facing {surface.field} ({surface.location}) contains "
            f"internal jargon or non-customer-appropriate language"
        )
        if verdict.explanation:
            message = f"{message}: {verdict.explanation}"
        findings.append(
            make_model_finding(
                rule_id=RULE_ID,
                file=rel,
                line=surface.line,
                message=message,
                evidence=verdict.evidence or surface.value,
                directives=directives,
            )
        )
    return findings

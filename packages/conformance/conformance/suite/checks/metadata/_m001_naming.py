"""M001 AppNamingConvention — model-driven check (BLDX-1575).

App and entrypoint names render in the marketplace and customer tenants, and the
fleet convention is to name an app after the *system/technology* it connects to
(e.g. "Redshift Miner"), not after the *vendor/company* that owns it (e.g. "Amazon
Redshift Miner").  Names drift from this: some carry a company-name prefix, some
do not.

This cannot be a deterministic check: recognising a company/vendor proper noun is
inherently fuzzy, and — critically — a committed wordlist of company names must
NOT live in this public repository.  So the model applies the convention from the
prompt alone, with no name list stored anywhere.
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

RULE_ID = "M001"

_SYSTEM = (
    "You review app and entrypoint names for a data-tooling marketplace. The "
    "convention is to name an app after the system or technology it integrates "
    "with (e.g. 'Redshift Miner', 'Snowflake Extractor'), NOT after the "
    "vendor/company that owns that system. Flag a name ONLY when it embeds a "
    "company or organization proper noun as a prefix or qualifier that the "
    "convention would omit (e.g. 'Amazon Redshift Miner', 'Microsoft SQL "
    "Extractor'). Do NOT flag the system/technology name itself, generic "
    "product words, or names that already follow the convention. When you flag, "
    "copy the exact offending span into 'evidence' and give a one-sentence "
    "reason in 'explanation'. When in doubt, do not flag."
)


def _user_prompt(surface: Surface) -> str:
    return f"Name field: {surface.field} ({surface.location})\nValue: {surface.value}"


def scan_surfaces(
    surfaces: list[Surface],
    rel: str,
    directives: dict[int, _IgnoreDirective],
    client: ModelClient,
) -> list[Finding]:
    """Classify each name surface and emit findings for convention violations."""
    findings: list[Finding] = []
    for surface in surfaces:
        if not surface.is_name:
            continue
        verdict = client.classify(
            system=_SYSTEM, user=_user_prompt(surface), schema=TextVerdict
        )
        if not verdict.flagged:
            continue
        message = (
            f"App {surface.field} ({surface.location}) does not follow the "
            f"naming convention"
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

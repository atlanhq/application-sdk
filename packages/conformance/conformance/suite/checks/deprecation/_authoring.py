"""SDK-side deprecation authoring hygiene — B002 / B003 / B004 (scope ``sdk``).

These run against the SDK's own source: they check that *the SDK declares its
deprecations correctly*, so every notice is actionable and none lingers past its
promised removal.

* **B002 ``MalformedDeprecationNotice``** — a deprecation *notice* whose message
  does not name *both* a migration target and a removal version.
* **B003 ``OverdueDeprecationRemoval``** — a deprecation *notice* whose stated
  removal version the SDK's current ``[project].version`` has already reached.
* **B004 ``UnmarkedDeprecationClaim``** — a *symbol* whose docstring *claims*
  deprecation but carries no machine-readable marker, so B001 / tooling cannot
  see it.

B002/B003 work at *notice* level (every ``@deprecated`` / ``DeprecationWarning``,
including those for deprecated parameters or modes) because a removal promise and
a migration target are properties of the notice, not of an importable symbol.
B004 works at *symbol* level because a docstring claim attaches to a symbol.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from .._version import parse_version, version_reached
from ._extractor import extract_notices, extract_sites


@dataclass(frozen=True)
class _Loc:
    """Minimal location object for ``make_finding`` (it reads ``lineno`` /
    ``col_offset`` only)."""

    lineno: int
    col_offset: int = 0


def _snippet(message: str, limit: int = 70) -> str:
    """A single-line, length-capped excerpt of a notice for identification."""
    flat = " ".join(message.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def scan_authoring(
    tree: ast.Module,
    file: str,
    current_version: str | None,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B002/B003/B004 findings for one SDK source module.

    *current_version* is the SDK's ``[project].version`` (for B003); when ``None``
    (version undeterminable) B003 is skipped — overdue-ness cannot be decided.
    """
    findings: list[Finding] = []
    current = parse_version(current_version) if current_version else None

    # B002 + B003 — notice-level.
    for notice in extract_notices(tree):
        loc = _Loc(lineno=notice.lineno)

        missing: list[str] = []
        if not notice.has_migration_target:
            missing.append("a migration target (e.g. 'use <replacement>')")
        if notice.removal_version_raw is None:
            missing.append("a removal version (e.g. 'will be removed in v4.0')")
        if missing:
            findings.append(
                make_finding(
                    filename=file,
                    rule_id="B002",
                    node=loc,
                    message=(
                        f"Deprecation notice is missing {' and '.join(missing)}. "
                        "A deprecation must tell callers what to use instead and when "
                        f'it will be removed. Notice: "{_snippet(notice.message)}".'
                    ),
                    directives=directives,
                )
            )

        if notice.removal_version_raw is not None and current is not None:
            removal = parse_version(notice.removal_version_raw)
            if removal is not None and version_reached(removal, current):
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id="B003",
                        node=loc,
                        message=(
                            f"Deprecation targets removal in v{notice.removal_version_raw}, "
                            f"but the SDK is already at v{current_version}. It is overdue "
                            "for removal — remove the deprecated surface or push out the "
                            f'removal version. Notice: "{_snippet(notice.message)}".'
                        ),
                        directives=directives,
                    )
                )

    # B004 — symbol-level claim with no enforcement of any kind.  A symbol whose
    # body emits DeprecationWarning is enforced (its notice may still be
    # malformed — that is B002's concern), so it is not an "unmarked" claim.
    for site in extract_sites(tree):
        if site.marker_via is None and site.docstring_claim and not site.emits_warning:
            findings.append(
                make_finding(
                    filename=file,
                    rule_id="B004",
                    node=_Loc(lineno=site.lineno),
                    message=(
                        f"'{site.symbol}' is documented as deprecated but carries no "
                        "machine-readable marker. Add @deprecated(...) (or emit "
                        "DeprecationWarning from __init__/__init_subclass__) so "
                        "consumers and B001 can detect it."
                    ),
                    directives=directives,
                )
            )

    return findings

"""Finding — the language-neutral result type for conformance checkers."""

from __future__ import annotations

from dataclasses import dataclass, field

from conformance.suite.schema.builder import ReportBuilder
from conformance.suite.schema.sarif import SarifReport, Suppression


@dataclass(frozen=True)
class Finding:
    """A single rule violation found by a checker.

    ``suppressed=True`` means the violation was acknowledged at the source via
    a ``# conformance: ignore[PXXX] <reason>`` directive.  Suppressed findings
    still appear in the SARIF output (as ``kind="fail"`` with a
    ``suppressions`` entry) so they are auditable, but they do not contribute
    to the failing count and therefore do not affect the gate exit code.

    ``discriminator`` distinguishes findings that share a rule and location.
    A rule that emits several findings anchored to one spot (e.g. T025 reports
    each uncovered bundle entrypoint at ``pyproject.toml:1``) sets it to the
    varying part (the entrypoint name), so fingerprints stay distinct and a
    ``# conformance: ignore[T025:<discriminator>]`` directive can suppress one
    finding without suppressing its siblings. ``None`` (the default) keeps the
    pre-discriminator fingerprint and directive behaviour.
    """

    rule_id: str
    file: str
    line: int
    column: int
    message: str
    snippet: str | None = None
    discriminator: str | None = field(default=None, compare=False, hash=False)
    suppressed: bool = field(default=False, compare=False, hash=False)
    suppression_justification: str | None = field(
        default=None, compare=False, hash=False
    )


def findings_to_report(
    findings: list[Finding],
    *,
    tool_version: str,
    repo_uri: str | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
    excluded_paths: list[str] | None = None,
    rule_ids: set[str] | None = None,
) -> SarifReport:
    """Convert a list of Findings to a SARIF SarifReport via ReportBuilder.

    ``rule_ids`` narrows the emitted ``driver.rules`` catalog to exactly the
    requested rules (a ``--rule``-scoped run should not ship 150+ descriptors
    for one rule's findings — consumers feed this SARIF to models, and the
    unused descriptors are pure context waste). ``None`` keeps the full
    catalog, which series-scoped runs rely on for fleet dashboards.
    """
    from conformance.suite.rules import load_catalog

    catalog = load_catalog()
    if rule_ids is not None:
        wanted = set(rule_ids) | {f.rule_id for f in findings}
        catalog = [r for r in catalog if r.id in wanted]
    builder = ReportBuilder.from_catalog(
        catalog,
        tool_name="atlan-conformance",
        tool_version=tool_version,
        repo_uri=repo_uri,
        commit_sha=commit_sha,
        branch=branch,
    )
    for f in findings:
        suppressions: list[Suppression] | None = None
        if f.suppressed:
            suppressions = [
                Suppression(
                    kind="inSource",
                    justification=f.suppression_justification or "",
                    status="accepted",
                )
            ]
        builder.add_result(
            rule_id=f.rule_id,
            file_uri=f.file,
            start_line=f.line,
            start_column=f.column,
            message=f.message,
            snippet=f.snippet,
            suppressions=suppressions,
            discriminator=f.discriminator,
        )
    return builder.build(excluded_paths=excluded_paths)

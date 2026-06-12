"""Finding — the language-neutral result type for conformance checkers."""

from __future__ import annotations

from dataclasses import dataclass, field

from suite.schema.builder import ReportBuilder
from suite.schema.sarif import SarifReport, Suppression


@dataclass(frozen=True)
class Finding:
    """A single rule violation found by a checker.

    ``suppressed=True`` means the violation was acknowledged at the source via
    a ``# conformance: ignore[PXXX] <reason>`` directive.  Suppressed findings
    still appear in the SARIF output (as ``kind="fail"`` with a
    ``suppressions`` entry) so they are auditable, but they do not contribute
    to the failing count and therefore do not affect the gate exit code.
    """

    rule_id: str
    file: str
    line: int
    column: int
    message: str
    snippet: str | None = None
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
) -> SarifReport:
    """Convert a list of Findings to a SARIF SarifReport via ReportBuilder."""
    from suite.rules import load_catalog

    catalog = load_catalog()
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
        )
    return builder.build()

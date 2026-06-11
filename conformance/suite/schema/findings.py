"""Finding — the language-neutral result type for conformance checkers."""

from __future__ import annotations

from dataclasses import dataclass

from suite.schema.builder import ReportBuilder
from suite.schema.sarif import SarifReport


@dataclass(frozen=True)
class Finding:
    """A single rule violation found by a checker."""

    rule_id: str
    file: str
    line: int
    column: int
    message: str
    snippet: str | None = None


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
        builder.add_result(
            rule_id=f.rule_id,
            file_uri=f.file,
            start_line=f.line,
            start_column=f.column,
            message=f.message,
            snippet=f.snippet,
        )
    return builder.build()

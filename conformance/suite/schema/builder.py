"""ReportBuilder — fluent builder for conformance SARIF reports.

Typical usage in a rule-checker::

    from suite.schema import ReportBuilder, load_catalog

    rules = load_catalog()
    builder = ReportBuilder.from_catalog(
        rules,
        tool_name="atlan-conformance",
        tool_version="3.16.0",
        repo_uri="https://github.com/atlanhq/my-app",
        commit_sha="abc123",
    )

    # Record a violation:
    builder.add_result(
        rule_id="P001",
        file_uri="src/connector/extractor.py",
        start_line=42,
        message="Bare 'except: pass' — silently discards all exceptions",
    )

    # Record a violation with inline suppression:
    from suite.schema.sarif import Suppression, Location, PhysicalLocation, ArtifactLocation, Region
    suppression = Suppression(
        kind="inSource",
        justification="optional-dep guard: PIL is never available in CI but is fine",
    )
    builder.add_result(
        rule_id="P001",
        file_uri="src/connector/extractor.py",
        start_line=87,
        suppressions=[suppression],
    )

    # Record a clean pass (e.g. after a rule ran and found nothing):
    builder.add_pass(rule_id="L001", file_uri="src/connector/extractor.py")

    report = builder.build()
    import json
    print(json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2))
"""

from __future__ import annotations

import hashlib
from typing import Any

from suite.schema.catalog import RuleDefinition
from suite.schema.disposition import Disposition, derive_disposition
from suite.schema.extensions import AtlanRunProperties, DispositionSummary
from suite.schema.sarif import (
    ArtifactLocation,
    Invocation,
    Location,
    PhysicalLocation,
    Region,
    ReportingDescriptor,
    Result,
    SarifReport,
    SarifRun,
    Suppression,
    Tool,
    ToolComponent,
    VersionControlDetails,
)


def _fingerprint(rule_id: str, uri: str, start_line: int) -> str:
    """Stable ``atlanConformance/v1`` fingerprint for a result.

    Hashed from (rule_id, normalised URI, start_line) so that:
    * the same finding across runs produces the same key (dedup / oscillation
      detection),
    * moving a block of code changes the line → new fingerprint (correct
      invalidation).
    """
    key = f"{rule_id}\x00{uri.lstrip('./')}\x00{start_line}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class ReportBuilder:
    """Fluent builder that accumulates results and emits a :class:`SarifReport`.

    Parameters
    ----------
    tool_name:
        Short name for the suite driver, e.g. ``"atlan-conformance"``.
    tool_version:
        SDK / suite version string, e.g. ``"3.16.0"``.
    rules:
        Ordered list of :class:`~conformance.schema.sarif.ReportingDescriptor`
        objects.  Use :meth:`from_catalog` to build these from
        :class:`~conformance.schema.catalog.RuleDefinition` objects.
    repo_uri:
        Optional repository URI for ``versionControlProvenance``.
    commit_sha:
        Optional commit SHA for ``versionControlProvenance``.
    branch:
        Optional branch name.
    """

    def __init__(
        self,
        tool_name: str,
        tool_version: str,
        rules: list[ReportingDescriptor] | None = None,
        repo_uri: str | None = None,
        commit_sha: str | None = None,
        branch: str | None = None,
    ) -> None:
        self._tool_name = tool_name
        self._tool_version = tool_version
        self._rules: list[ReportingDescriptor] = rules or []
        self._rule_index: dict[str, int] = {r.id: i for i, r in enumerate(self._rules)}
        # Resolved default level per rule — used to make results self-describing
        # (SARIF allows level=None meaning "inherit", but derive_disposition needs
        # an explicit value to avoid defaulting everything to "warning").
        self._rule_levels: dict[str, str] = {
            r.id: r.default_configuration.level for r in self._rules
        }
        self._results: list[Result] = []
        self._repo_uri = repo_uri
        self._commit_sha = commit_sha
        self._branch = branch

    @classmethod
    def from_catalog(
        cls,
        catalog: list[RuleDefinition],
        tool_name: str,
        tool_version: str,
        repo_uri: str | None = None,
        commit_sha: str | None = None,
        branch: str | None = None,
    ) -> ReportBuilder:
        """Create a builder pre-loaded with descriptors from a rule catalog."""
        descriptors = [r.to_reporting_descriptor() for r in catalog]
        return cls(
            tool_name=tool_name,
            tool_version=tool_version,
            rules=descriptors,
            repo_uri=repo_uri,
            commit_sha=commit_sha,
            branch=branch,
        )

    def add_result(
        self,
        rule_id: str,
        file_uri: str,
        start_line: int,
        *,
        start_column: int = 1,
        end_line: int | None = None,
        end_column: int | None = None,
        message: str = "",
        level: str | None = None,
        suppressions: list[Suppression] | None = None,
        hint: str | None = None,
        snippet: str | None = None,
        extra_properties: dict[str, Any] | None = None,
    ) -> ReportBuilder:
        """Record a rule violation (``kind="fail"``).

        Parameters
        ----------
        rule_id:
            Must match an entry in the rule catalog passed to the builder.
        file_uri:
            Repo-root-relative path, e.g. ``"src/connector/extractor.py"``.
        start_line:
            1-based line number of the violation.
        level:
            Override the rule's default level (``"error"`` or ``"warning"``).
            If omitted, the effective level is inherited from the rule's
            ``defaultConfiguration.level``.
        suppressions:
            Non-empty → disposition becomes ``SUPPRESSED``.
        hint:
            Machine-actionable remediation hint (stored in ``atlan/hint``).
        snippet:
            Short source snippet for context (stored in ``region.snippet``).
        extra_properties:
            Additional ``atlan/*`` properties to merge into ``result.properties``.
        """
        from suite.schema.extensions import AtlanResultProperties

        region = Region(
            startLine=start_line,
            startColumn=start_column,
            endLine=end_line,
            endColumn=end_column,
            snippet={"text": snippet} if snippet else None,
        )
        location = Location(
            physicalLocation=PhysicalLocation(
                artifactLocation=ArtifactLocation(uri=file_uri),
                region=region,
            )
        )
        fingerprint = _fingerprint(rule_id, file_uri, start_line)
        props: dict[str, Any] = AtlanResultProperties(hint=hint).to_properties()
        if extra_properties:
            props.update(extra_properties)

        # Resolve effective level: explicit override → rule default → "warning".
        # Setting it explicitly makes results self-describing so derive_disposition
        # does not need access to the rule catalog.
        effective_level = level or self._rule_levels.get(rule_id, "warning")

        result = Result(
            ruleId=rule_id,
            ruleIndex=self._rule_index.get(rule_id),
            kind="fail",
            level=effective_level,
            message={"text": message},
            locations=[location],
            partialFingerprints={"atlanConformance/v1": fingerprint},
            suppressions=suppressions or [],
            properties=props,
        )
        self._results.append(result)
        return self

    def add_pass(
        self,
        rule_id: str,
        file_uri: str,
        *,
        message: str = "",
    ) -> ReportBuilder:
        """Record an explicit pass for a rule against a specific file.

        Emitting explicit pass results is optional but useful when consumers
        need to verify that a rule *ran* against a file, not just that no
        failure was found.
        """
        result = Result(
            ruleId=rule_id,
            ruleIndex=self._rule_index.get(rule_id),
            kind="pass",
            message={"text": message},
            locations=[
                Location(
                    physicalLocation=PhysicalLocation(
                        artifactLocation=ArtifactLocation(uri=file_uri),
                    )
                )
            ],
        )
        self._results.append(result)
        return self

    def build(self, *, exit_code: int | None = None) -> SarifReport:
        """Finalise and return the :class:`SarifReport`.

        Computes the ``atlan/summary`` disposition counts and sets the
        ``invocation.exitCode`` (0 = gate passed; 1 = gate failed) unless
        overridden by the caller.

        The ``exit_code`` is derived automatically as:
        * 0 — no ``FAILING`` results
        * 1 — ≥1 ``FAILING`` result

        Pass an explicit ``exit_code`` only when the caller has additional
        gating logic beyond per-result disposition.
        """
        # Compute summary
        summary = DispositionSummary()
        for result in self._results:
            disposition = derive_disposition(result)
            if disposition == Disposition.PASS:
                summary.passing += 1
            elif disposition == Disposition.FAILING:
                summary.failing += 1
            elif disposition == Disposition.WARNING:
                summary.warning += 1
            elif disposition == Disposition.SUPPRESSED:
                summary.suppressed += 1

        # Gate decision
        derived_exit_code = 1 if summary.failing > 0 else 0
        effective_exit_code = exit_code if exit_code is not None else derived_exit_code

        # Version-control provenance
        provenance: list[VersionControlDetails] = []
        if self._repo_uri:
            provenance.append(
                VersionControlDetails(
                    repositoryUri=self._repo_uri,
                    revisionId=self._commit_sha,
                    branch=self._branch,
                )
            )

        run_props = AtlanRunProperties(summary=summary)

        run = SarifRun(
            tool=Tool(
                driver=ToolComponent(
                    name=self._tool_name,
                    version=self._tool_version,
                    semanticVersion=self._tool_version,
                    rules=list(self._rules),
                )
            ),
            results=list(self._results),
            invocations=[
                Invocation(
                    executionSuccessful=True,
                    exitCode=effective_exit_code,
                    exitCodeDescription=(
                        "Gate failed: one or more blocking violations found."
                        if effective_exit_code != 0
                        else "Gate passed."
                    ),
                )
            ],
            versionControlProvenance=provenance,
            properties=run_props.to_properties(),
        )

        return SarifReport(runs=[run])

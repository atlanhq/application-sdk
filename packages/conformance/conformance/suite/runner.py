"""Conformance suite runner — dispatches registered checks, optionally filtered by series.

Usage::

    # Run everything
    python -m conformance.suite.runner --repo . --output report.sarif

    # Run only CI/workflow checks (C-series)
    python -m conformance.suite.runner --repo . --series C --output ci.sarif

    # Run multiple series
    python -m conformance.suite.runner --repo . --series C,E --output code.sarif
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import conformance.suite.checks.entrypoint as entrypoint
import conformance.suite.checks.logging as logging_checks
import conformance.suite.checks.sdr as sdr_checks
import conformance.suite.checks.sdr_test_checks as sdr_test_checks
from conformance.suite.checks import (
    actions_pinning,
    app_name_alignment,
    artifact_schema_declared,
    artifact_schema_writer,
    asyncio_loop_scope,
    bootstrap_drift,
    client_seam,
    coverage_config,
    dependency_conformance,
    deprecation,
    determinism,
    dev_entrypoint,
    dockerfile_conformance,
    download_retry,
    e2e_agent_spec,
    e2e_deployment_name,
    e2e_generated_harness,
    e2e_workflow_shape,
    entrypoint_alignment,
    entrypoint_e2e_coverage,
    error_handling,
    error_seam,
    generated_freshness,
    gitignore_entries,
    integration_deselect,
    integration_marking,
    legacy_contract,
    manifest_app_name,
    manifest_contract,
    optimizations,
    orchestration,
    persistence_seam,
    preflight,
    prescriptions,
    release_contract,
    security,
    test_quality,
    test_structure,
    text_io_encoding,
    transform_templates,
)
from conformance.suite.checks._ast_common import TOOL_VERSION, detect_scope
from conformance.suite.rules import CATALOG, assert_registry_consistent, get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding, findings_to_report


@dataclass(frozen=True)
class CheckRegistration:
    """A registered check module, identified by its rule-series letter.

    A check supplies either ``scan_path`` (per-file scanning) or ``scan_all``
    (cross-file scanning that needs to see every file before emitting findings —
    e.g. transitive inheritance resolution).  When ``scan_all`` is set the
    runner calls it once with the post-exclusion path list; otherwise it calls
    ``scan_path`` per file.
    """

    series: str
    discover: Callable[[Path], list[Path]]
    scan_path: Callable[[Path, Path], list[Finding]]
    scan_all: Callable[[list[Path], Path], list[Finding]] | None = None


_CHECKS: list[CheckRegistration] = [
    CheckRegistration(
        series=actions_pinning.SERIES,
        discover=actions_pinning.discover,
        scan_path=actions_pinning.scan_path,
    ),
    CheckRegistration(
        series=asyncio_loop_scope.SERIES,
        discover=asyncio_loop_scope.discover,
        scan_path=asyncio_loop_scope.scan_path,
    ),
    CheckRegistration(
        series=bootstrap_drift.SERIES,
        discover=bootstrap_drift.discover,
        scan_path=bootstrap_drift.scan_path,
    ),
    CheckRegistration(
        series=client_seam.SERIES,
        discover=client_seam.discover,
        scan_path=client_seam.scan_path,
    ),
    CheckRegistration(
        series=persistence_seam.SERIES,
        discover=persistence_seam.discover,
        scan_path=persistence_seam.scan_path,
    ),
    CheckRegistration(
        series=download_retry.SERIES,
        discover=download_retry.discover,
        scan_path=download_retry.scan_path,
    ),
    CheckRegistration(
        series=error_handling.SERIES,
        discover=error_handling.discover,
        scan_path=error_handling.scan_path,
    ),
    CheckRegistration(
        series=error_seam.SERIES,
        discover=error_seam.discover,
        scan_path=error_seam.scan_path,
    ),
    CheckRegistration(
        series=dependency_conformance.SERIES,
        discover=dependency_conformance.discover,
        scan_path=dependency_conformance.scan_path,
        scan_all=dependency_conformance.scan_all,
    ),
    CheckRegistration(
        series=optimizations.SERIES,
        discover=optimizations.discover,
        scan_path=optimizations.scan_path,
    ),
    CheckRegistration(
        series=prescriptions.SERIES,
        discover=prescriptions.discover,
        scan_path=prescriptions.scan_path,
        scan_all=prescriptions.scan_all,
    ),
    CheckRegistration(
        series=gitignore_entries.SERIES,
        discover=gitignore_entries.discover,
        scan_path=gitignore_entries.scan_path,
    ),
    CheckRegistration(
        series=orchestration.SERIES,
        discover=orchestration.discover,
        scan_path=orchestration.scan_path,
        scan_all=orchestration.scan_all,
    ),
    CheckRegistration(
        series=determinism.SERIES,
        discover=determinism.discover,
        scan_path=determinism.scan_path,
    ),
    CheckRegistration(
        series=entrypoint.SERIES,
        discover=entrypoint.discover,
        scan_path=entrypoint.scan_path,
    ),
    CheckRegistration(
        series=logging_checks.SERIES,
        discover=logging_checks.discover,
        scan_path=logging_checks.scan_path,
        scan_all=logging_checks.scan_all,
    ),
    CheckRegistration(
        series=integration_marking.SERIES,
        discover=integration_marking.discover,
        scan_path=integration_marking.scan_path,
    ),
    CheckRegistration(
        series=test_quality.SERIES,
        discover=test_quality.discover,
        scan_path=test_quality.scan_path,
    ),
    CheckRegistration(
        series=test_structure.SERIES,
        discover=test_structure.discover,
        scan_path=test_structure.scan_path,
        scan_all=test_structure.scan_all,
    ),
    CheckRegistration(
        series=coverage_config.SERIES,
        discover=coverage_config.discover,
        scan_path=coverage_config.scan_path,
    ),
    CheckRegistration(
        series=integration_deselect.SERIES,
        discover=integration_deselect.discover,
        scan_path=integration_deselect.scan_path,
    ),
    CheckRegistration(
        series=e2e_deployment_name.SERIES,
        discover=e2e_deployment_name.discover,
        scan_path=e2e_deployment_name.scan_path,
    ),
    CheckRegistration(
        series=e2e_agent_spec.SERIES,
        discover=e2e_agent_spec.discover,
        scan_path=e2e_agent_spec.scan_path,
    ),
    CheckRegistration(
        series=e2e_workflow_shape.SERIES,
        discover=e2e_workflow_shape.discover,
        scan_path=e2e_workflow_shape.scan_path,
        scan_all=e2e_workflow_shape.scan_all,
    ),
    CheckRegistration(
        series=e2e_generated_harness.SERIES,
        discover=e2e_generated_harness.discover,
        scan_path=e2e_generated_harness.scan_path,
        scan_all=e2e_generated_harness.scan_all,
    ),
    CheckRegistration(
        series=entrypoint_e2e_coverage.SERIES,
        discover=entrypoint_e2e_coverage.discover,
        scan_path=entrypoint_e2e_coverage.scan_path,
        scan_all=entrypoint_e2e_coverage.scan_all,
    ),
    CheckRegistration(
        series=dockerfile_conformance.SERIES,
        discover=dockerfile_conformance.discover,
        scan_path=dockerfile_conformance.scan_path,
    ),
    CheckRegistration(
        series=deprecation.SERIES,
        discover=deprecation.discover,
        scan_path=deprecation.scan_path,
        scan_all=deprecation.scan_all,
    ),
    CheckRegistration(
        series=entrypoint_alignment.SERIES,
        discover=entrypoint_alignment.discover,
        scan_path=entrypoint_alignment.scan_path,
        scan_all=entrypoint_alignment.scan_all,
    ),
    CheckRegistration(
        series=app_name_alignment.SERIES,
        discover=app_name_alignment.discover,
        scan_path=app_name_alignment.scan_path,
        scan_all=app_name_alignment.scan_all,
    ),
    CheckRegistration(
        series=preflight.SERIES,
        discover=preflight.discover,
        scan_path=preflight.scan_path,
        scan_all=preflight.scan_all,
    ),
    CheckRegistration(
        series=legacy_contract.SERIES,
        discover=legacy_contract.discover,
        scan_path=legacy_contract.scan_path,
    ),
    CheckRegistration(
        series=generated_freshness.SERIES,
        discover=generated_freshness.discover,
        scan_path=generated_freshness.scan_path,
        scan_all=generated_freshness.scan_all,
    ),
    CheckRegistration(
        series=artifact_schema_declared.SERIES,
        discover=artifact_schema_declared.discover,
        scan_path=artifact_schema_declared.scan_path,
        scan_all=artifact_schema_declared.scan_all,
    ),
    CheckRegistration(
        series=artifact_schema_writer.SERIES,
        discover=artifact_schema_writer.discover,
        scan_path=artifact_schema_writer.scan_path,
        scan_all=artifact_schema_writer.scan_all,
    ),
    CheckRegistration(
        series=manifest_contract.SERIES,
        discover=manifest_contract.discover,
        scan_path=manifest_contract.scan_path,
        scan_all=manifest_contract.scan_all,
    ),
    CheckRegistration(
        series=manifest_app_name.SERIES,
        discover=manifest_app_name.discover,
        scan_path=manifest_app_name.scan_path,
        scan_all=manifest_app_name.scan_all,
    ),
    CheckRegistration(
        series=release_contract.SERIES,
        discover=release_contract.discover,
        scan_path=release_contract.scan_path,
        scan_all=release_contract.scan_all,
    ),
    CheckRegistration(
        series=sdr_checks.SERIES,
        discover=sdr_checks.discover,
        scan_path=sdr_checks.scan_path,
        scan_all=sdr_checks.scan_all,
    ),
    CheckRegistration(
        series=transform_templates.SERIES,
        discover=transform_templates.discover,
        scan_path=transform_templates.scan_path,
    ),
    CheckRegistration(
        series=sdr_test_checks.SERIES,
        discover=sdr_test_checks.discover,
        scan_path=sdr_test_checks.scan_path,
        scan_all=sdr_test_checks.scan_all,
    ),
    CheckRegistration(
        series=dev_entrypoint.SERIES,
        discover=dev_entrypoint.discover,
        scan_path=dev_entrypoint.scan_path,
    ),
    CheckRegistration(
        series=security.SERIES,
        discover=security.discover,
        scan_path=security.scan_path,
    ),
    CheckRegistration(
        series=text_io_encoding.SERIES,
        discover=text_io_encoding.discover,
        scan_path=text_io_encoding.scan_path,
    ),
]


# Registry invariant: every registered checker's series must have rule
# definitions in the catalog (so get_rule() resolves for each finding it emits).
assert_registry_consistent(check_series=frozenset(c.series for c in _CHECKS))


def _tier(f: Finding) -> EnforcementTier:
    return get_rule(f.rule_id).tier


def _rule_in_scope(rule_scope: RuleScope, active: RuleScope | None) -> bool:
    """True if a rule with ``rule_scope`` applies under the ``active`` scope.

    ``active`` is ``None`` when the scope could not be determined and ``--scope``
    was not given — in that case every rule is in scope (the pre-feature
    behaviour).  Otherwise a rule applies iff its scope is ``BOTH`` or matches.
    """
    return active is None or rule_scope == RuleScope.BOTH or rule_scope == active


def _series_in_scope(series: str, active: RuleScope | None) -> bool:
    """True if *any* rule in ``series`` is in scope under ``active``.

    Used to skip a whole check's discovery+scan when none of its series' rules
    could ever produce an in-scope finding (e.g. the all-APP D-series on the SDK).
    Series that mix scopes (C001/C003 are ``both`` while C002 is ``app``) stay
    active and rely on the post-scan finding filter for correctness.
    """
    return any(
        _rule_in_scope(rule.scope, active)
        for rule in CATALOG.values()
        if rule.id[0] == series
    )


def _print_human_summary(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    """Print a human-readable violation/warning summary to stdout."""
    label = f"{series}-series" if series else "conformance suite"
    active = [f for f in findings if not f.suppressed]
    suppressed = [f for f in findings if f.suppressed]
    excluded_note = (
        f" (excluded: {', '.join(sorted(excluded_prefixes))})"
        if excluded_prefixes
        else ""
    )
    if not active and not suppressed:
        print(f"conformance ({label}): no violations found.{excluded_note}")
        return
    blocking = [f for f in active if _tier(f) == EnforcementTier.BLOCK]
    warnings = [f for f in active if _tier(f) == EnforcementTier.WARN]
    parts = []
    if blocking:
        parts.append(f"{len(blocking)} violation{'s' if len(blocking) != 1 else ''}")
    if warnings:
        parts.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    if suppressed:
        parts.append(f"{len(suppressed)} suppressed")
    if not parts:
        print(f"conformance ({label}): no violations found.{excluded_note}")
        return
    print(f"conformance ({label}): {', '.join(parts)} found.{excluded_note}\n")
    for f in active:
        level = "FAIL" if _tier(f) == EnforcementTier.BLOCK else "WARN"
        print(f"  [{f.rule_id}] [{level}] {f.file}:{f.line}:{f.column}")
        print(f"  {f.message}\n")


def _pct(msg: str) -> str:
    """Percent-encode special characters per the GitHub Actions workflow-command spec."""
    return msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit_github_summary_annotations(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
    *,
    exit_zero: bool = False,
) -> None:
    """Emit at most four summary annotations to the GitHub Actions log.

    Per-finding annotations are capped at 10 per type by GitHub without any
    visible indication of truncation, making it impossible to track overall
    progress from the PR view.  Instead we emit one annotation per non-zero
    category (blocking / warning / suppressing), each carrying the full count
    and a link to the workflow run where the SARIF artifact can be downloaded.

    A fourth ``::notice`` is emitted when paths were excluded from scanning,
    so the reduced scope is always visible alongside the finding counts.

    When ``exit_zero=True`` (soft-enforcement mode), blocking violations are
    downgraded from ``::error`` to ``::warning`` so the annotation severity
    matches the job exit code — preventing red error banners on a green job.

    Emits nothing when there are no findings and no exclusions.
    """
    blocking = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.BLOCK
    ]
    warns = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.WARN
    ]
    suppressed = [f for f in findings if f.suppressed]

    if not blocking and not warns and not suppressed and not excluded_prefixes:
        return

    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if repo and run_id:
        detail = f"Full report: {server}/{repo}/actions/runs/{run_id} (download the SARIF artifact)."
    else:
        detail = "Download the SARIF artifact from this workflow run for full details."

    label = f"{series}-series" if series else "conformance suite"

    if blocking:
        n = len(blocking)
        msg = _pct(
            f"{n} blocking violation{'s' if n != 1 else ''} found by {label}. {detail}"
        )
        # Soft mode: job exits 0, so downgrade annotation to ::warning to match.
        level = "warning" if exit_zero else "error"
        print(
            f"::{level} title=Conformance: {n} blocking violation{'s' if n != 1 else ''} ({label})::{msg}"
        )

    if warns:
        n = len(warns)
        msg = _pct(f"{n} warning{'s' if n != 1 else ''} found by {label}. {detail}")
        print(
            f"::warning title=Conformance: {n} warning{'s' if n != 1 else ''} ({label})::{msg}"
        )

    if suppressed:
        n = len(suppressed)
        msg = _pct(
            f"{n} suppressed finding{'s' if n != 1 else ''} in {label}. {detail}"
        )
        print(f"::notice title=Conformance: {n} suppressed ({label})::{msg}")

    if excluded_prefixes:
        paths_str = ", ".join(sorted(excluded_prefixes))
        msg = _pct(
            f"{label} excluded from scanning: {paths_str}. "
            f"Findings in these paths are not counted. {detail}"
        )
        print(f"::notice title=Conformance: excluded paths ({label})::{msg}")


def parse_rule_ids(raw: str) -> set[str]:
    """Validate a comma-separated ``--rule`` value against the catalog.

    Unknown ids fail loudly here rather than yielding a silently-empty run —
    the exact bug class ``--series L004`` used to cause (a series letter match
    against a full rule id selected zero checks and reported a clean repo).
    """
    from conformance.suite.rules import CATALOG  # noqa: PLC0415

    ids = {r.strip().upper() for r in raw.split(",") if r.strip()}
    if not ids:
        raise SystemExit("--rule was provided but contained no rule ids")
    unknown = sorted(i for i in ids if i not in CATALOG)
    if unknown:
        raise SystemExit(
            f"unknown rule id(s): {', '.join(unknown)} — "
            "see conformance/docs/rules/ for the catalog"
        )
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atlan conformance suite runner.")
    parser.add_argument("--repo", default=".", metavar="DIR")
    parser.add_argument(
        "--output", metavar="FILE", help="Write SARIF to FILE (default: stdout)"
    )
    parser.add_argument("--tool-version", default=TOOL_VERSION, metavar="VERSION")
    parser.add_argument(
        "--series",
        metavar="LETTERS",
        help=(
            "Comma-separated series letters to run, e.g. 'C' or 'C,E'. "
            "Default: all registered checks."
        ),
    )
    parser.add_argument(
        "--rule",
        metavar="IDS",
        help=(
            "Comma-separated rule ids to run, e.g. 'E010' or 'L004,E010'. "
            "The series is derived from the ids; findings, the exit code and "
            "the emitted SARIF rule catalog are scoped to exactly these rules. "
            "Mutually exclusive with --series."
        ),
    )
    parser.add_argument(
        "--exclude",
        metavar="PATHS",
        default="",
        help=(
            "Comma-separated repo-root-relative path prefixes to exclude from all "
            "checks, e.g. 'tools/,contract-toolkit/'.  Applies after discovery, so "
            "it works uniformly across every registered series."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=[RuleScope.SDK.value, RuleScope.APP.value],
        default=None,
        help=(
            "Restrict to rules that apply to this consumer surface ('sdk' or "
            "'app'); 'both'-scoped rules always run.  Default: auto-detected from "
            "the repo's [project].name (atlan-application-sdk* -> sdk, else app); "
            "if undetectable, every rule runs."
        ),
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        default=False,
        help=(
            "Always exit 0, even when blocking violations are found. "
            "The SARIF invocation.exitCode still reflects the real result so "
            "Security tab tracking is unaffected. "
            "Use during soft-enforcement rollouts where violations should be "
            "observed and tracked but must not block merges."
        ),
    )
    args = parser.parse_args(argv)

    rule_ids: set[str] | None = None
    if args.rule:
        if args.series:
            parser.error(
                "--rule and --series are mutually exclusive (--rule implies its series)"
            )
        rule_ids = parse_rule_ids(args.rule)
        requested = {rid[0] for rid in rule_ids}
        active = [c for c in _CHECKS if c.series in requested]
    elif args.series:
        requested = {s.strip().upper() for s in args.series.split(",")}
        active = [c for c in _CHECKS if c.series in requested]
    else:
        active = list(_CHECKS)

    # Build the set of excluded prefixes once (normalised, non-empty only).
    # Trailing slashes are stripped so matching is done on path-component
    # boundaries — 'tools' excludes 'tools/' and the file 'tools' itself,
    # but never 'tools_extra/'.
    excluded_prefixes = tuple(
        p.strip().lstrip("/").rstrip("/") for p in args.exclude.split(",") if p.strip()
    )

    root = Path(args.repo).resolve()

    # Resolve the active scope: an explicit --scope wins; otherwise auto-detect
    # from the repo under scan.  ``None`` means "scope unknown — run everything".
    active_scope = RuleScope(args.scope) if args.scope else detect_scope(root)

    all_findings: list[Finding] = []
    for check in active:
        # Skip a whole check when none of its series' rules could be in scope
        # (e.g. the all-APP D-series when scanning the SDK itself).
        if not _series_in_scope(check.series, active_scope):
            continue
        paths: list[Path] = []
        for p in check.discover(root):
            if excluded_prefixes:
                rel = p.relative_to(root).as_posix()
                if any(
                    rel == prefix or rel.startswith(prefix + "/")
                    for prefix in excluded_prefixes
                ):
                    continue
            paths.append(p)
        if check.scan_all is not None:
            all_findings.extend(check.scan_all(paths, root))
        else:
            for p in paths:
                all_findings.extend(check.scan_path(p, root))

    # Drop findings for rules outside the active scope.  This is the
    # finding-level counterpart to the series-level skip above: it covers
    # mixed-scope series (e.g. C, where C001 is 'both' but C002/C003 are 'app')
    # so the SARIF report, counts, and exit code reflect only in-scope rules.
    all_findings = [
        f
        for f in all_findings
        if _rule_in_scope(get_rule(f.rule_id).scope, active_scope)
    ]

    # --rule: keep exactly the requested rules. The series module may have
    # scanned siblings in the same pass; they are out of this run's contract.
    if rule_ids is not None:
        all_findings = [f for f in all_findings if f.rule_id in rule_ids]

    # Always surface violations in a human-readable form so CI logs are actionable.
    _print_human_summary(all_findings, args.rule or args.series, excluded_prefixes)

    if os.getenv("GITHUB_ACTIONS") == "true":
        _emit_github_summary_annotations(
            all_findings,
            args.rule or args.series,
            excluded_prefixes,
            exit_zero=args.exit_zero,
        )

    report = findings_to_report(
        all_findings,
        tool_version=args.tool_version,
        excluded_paths=list(excluded_prefixes),
        rule_ids=rule_ids,
    )
    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    sarif_exit_code: int = report.runs[0].invocations[0].exit_code  # type: ignore[assignment]
    return 0 if args.exit_zero else sarif_exit_code


if __name__ == "__main__":
    sys.exit(main())

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import conformance.suite.checks.logging as logging_checks
import conformance.suite.checks.sdr as sdr_checks
from conformance.suite.checks import (
    actions_pinning,
    app_name_alignment,
    artifact_schema_declared,
    artifact_schema_writer,
    asyncio_loop_scope,
    atomic_publish,
    bootstrap_drift,
    client_lifecycle,
    client_seam,
    coverage_config,
    credential_seam,
    dapr_objectstore,
    dapr_secretstore,
    dependency_conformance,
    deprecation,
    determinism,
    dev_entrypoint,
    dockerfile_conformance,
    download_retry,
    e2e_agent_spec,
    e2e_deployment_name,
    e2e_generated_harness,
    e2e_system_app_base,
    e2e_workflow_shape,
    entrypoint,
    entrypoint_alignment,
    entrypoint_e2e_coverage,
    error_handling,
    error_seam,
    generated_freshness,
    gitignore_entries,
    httpx_proxy,
    integration_deselect,
    integration_marking,
    legacy_contract,
    manifest_app_name,
    manifest_contract,
    offload_cancel,
    optimizations,
    orchestration,
    persistence_seam,
    preflight,
    prescriptions,
    release_contract,
    retry_ladder,
    sdr_test_checks,
    security,
    test_quality,
    test_structure,
    text_io_encoding,
    transform_templates,
)
from conformance.suite.checks._ast_common import (
    TOOL_VERSION,
    ParseFailure,
    detect_scope,
    parse_failure,
    source_cache,
)
from conformance.suite.checks.preflight._lifetime import scan_removed_config
from conformance.suite.introduced import BaseUnavailable, introduced_since
from conformance.suite.rules import CATALOG, assert_registry_consistent, get_rule
from conformance.suite.rules.preflight import RULES as PREFLIGHT_RULES
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding, findings_to_report
from conformance.suite.schema.sarif import (
    ArtifactLocation,
    Location,
    Notification,
    PhysicalLocation,
    Region,
)

# reportingDescriptorReference id on the notification for an unparseable file.
PARSE_FAILURE_ID = "conformance/parse-failure"

# Default for ``--since``. CI sets it once on the job, so every per-series
# ``detect`` step in that job gates on the change without each one passing it.
SINCE_ENV = "CONFORMANCE_SINCE"

# Per-file parse-failure annotations emitted before the aggregate one stands in
# for the rest. GitHub keeps at most 10 annotations of each level per step and
# drops the rest silently. The other summaries can use up to 2 of the same level
# (blocking + warning under --exit-zero), and the aggregate needs 1, so 7 leaves
# room for all of them.
_MAX_PARSE_FILE_ANNOTATIONS = 7


@dataclass(frozen=True)
class CheckRegistration:
    """A registered check module, identified by its rule-series letter.

    A check supplies ``scan_path`` (per-file scanning), ``scan_all`` (cross-file
    scanning that needs to see every file before emitting findings — e.g.
    transitive inheritance resolution), or both.  When ``scan_all`` is set the
    runner calls it once with the post-exclusion path list and never calls
    ``scan_path``; otherwise it calls ``scan_path`` per file.
    """

    series: str
    discover: Callable[[Path], list[Path]]
    scan_path: Callable[[Path, Path], list[Finding]] | None = None
    scan_all: Callable[[list[Path], Path], list[Finding]] | None = None

    def __post_init__(self) -> None:
        if self.scan_path is None and self.scan_all is None:
            raise TypeError(
                f"check series {self.series!r} defines neither scan_path nor scan_all"
            )

    @classmethod
    def from_module(cls, module: ModuleType) -> CheckRegistration:
        """Register a check module by the names every check module exports.

        ``SERIES`` and ``discover`` are required; ``scan_path`` and ``scan_all``
        are read when the module defines them.
        """
        return cls(
            series=module.SERIES,
            discover=module.discover,
            scan_path=getattr(module, "scan_path", None),
            scan_all=getattr(module, "scan_all", None),
        )

    def scan(self, paths: list[Path], root: Path) -> list[Finding]:
        """Run the check over its post-exclusion *paths*."""
        if self.scan_all is not None:
            return self.scan_all(paths, root)
        assert self.scan_path is not None  # guaranteed by __post_init__
        findings: list[Finding] = []
        for path in paths:
            findings.extend(self.scan_path(path, root))
        return findings


# Every check module the runner dispatches, in dispatch order.  A module under
# ``conformance/suite/checks`` that defines ``SERIES`` must appear here:
# ``tests/test_runner_registration.py`` fails otherwise, so a new check cannot
# be documented and unit-tested yet never run.
_CHECK_MODULES: tuple[ModuleType, ...] = (
    actions_pinning,
    asyncio_loop_scope,
    bootstrap_drift,
    client_seam,
    client_lifecycle,
    offload_cancel,
    httpx_proxy,
    persistence_seam,
    credential_seam,
    download_retry,
    error_handling,
    error_seam,
    dependency_conformance,
    optimizations,
    prescriptions,
    gitignore_entries,
    orchestration,
    determinism,
    entrypoint,
    logging_checks,
    integration_marking,
    test_quality,
    test_structure,
    coverage_config,
    integration_deselect,
    e2e_deployment_name,
    e2e_agent_spec,
    e2e_workflow_shape,
    e2e_generated_harness,
    entrypoint_e2e_coverage,
    e2e_system_app_base,
    dockerfile_conformance,
    deprecation,
    entrypoint_alignment,
    app_name_alignment,
    preflight,
    legacy_contract,
    generated_freshness,
    artifact_schema_declared,
    artifact_schema_writer,
    manifest_contract,
    manifest_app_name,
    release_contract,
    dapr_objectstore,
    dapr_secretstore,
    sdr_checks,
    transform_templates,
    sdr_test_checks,
    dev_entrypoint,
    security,
    text_io_encoding,
    atomic_publish,
    retry_ladder,
)

_CHECKS: list[CheckRegistration] = [
    CheckRegistration.from_module(module) for module in _CHECK_MODULES
]

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


def _print_parse_failures(
    parse_failures: list[ParseFailure], label: str, *, gating: bool = True
) -> None:
    """List the files no check scanned, so a clean summary is never a lie."""
    if not parse_failures:
        return
    tag = "FAIL" if gating else "WARN"
    n = len(parse_failures)
    print(
        f"conformance ({label}): {n} file{'s' if n != 1 else ''} could not be "
        f"parsed, so no check scanned {'them' if n != 1 else 'it'}.\n"
    )
    for failure in parse_failures:
        print(f"  [PARSE] [{tag}] {failure.file}:{failure.line}:{failure.column}")
        print(f"  {failure.message}\n")


def _print_human_summary(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
    parse_failures: list[ParseFailure] | None = None,
    *,
    parse_failures_gate: bool = True,
) -> None:
    """Print a human-readable violation/warning summary to stdout."""
    label = f"{series}-series" if series else "conformance suite"
    _print_parse_failures(parse_failures or [], label, gating=parse_failures_gate)
    active = [f for f in findings if not f.suppressed]
    suppressed = [f for f in findings if f.suppressed]
    excluded_note = (
        f" (excluded: {', '.join(sorted(excluded_prefixes))})"
        if excluded_prefixes
        else ""
    )
    if parse_failures:
        excluded_note += " (files listed above were not scanned)"
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


def _pct_prop(value: str) -> str:
    """Percent-encode a workflow-command *property* value (``file=``, ``title=``).

    Properties also need ``:`` and ``,`` escaped: unescaped, a repo-controlled
    file name could end the property list early or, with a newline, start a
    forged workflow command of its own.
    """
    return _pct(value).replace(":", "%3A").replace(",", "%2C")


def _emit_github_summary_annotations(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
    *,
    exit_zero: bool = False,
    parse_failures: list[ParseFailure] | None = None,
    parse_failures_gate: bool = True,
) -> None:
    """Emit summary annotations to the GitHub Actions log.

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

    Files that could not be parsed get one aggregate annotation with the full
    count, plus a file-anchored annotation each for the first
    :data:`_MAX_PARSE_FILE_ANNOTATIONS`: there is no SARIF result for such a
    file, so this is its only mark on the PR. The aggregate is what survives
    GitHub's cap. They are ``::error`` only when they gate the run and
    ``exit_zero`` is off.

    Emits nothing when there are no findings, no exclusions and no parse
    failures.
    """
    parse_failures = parse_failures or []
    blocking = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.BLOCK
    ]
    warns = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.WARN
    ]
    suppressed = [f for f in findings if f.suppressed]

    if (
        not blocking
        and not warns
        and not suppressed
        and not excluded_prefixes
        and not parse_failures
    ):
        return

    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if repo and run_id:
        detail = f"Full report: {server}/{repo}/actions/runs/{run_id} (download the SARIF artifact)."
    else:
        detail = "Download the SARIF artifact from this workflow run for full details."

    label = f"{series}-series" if series else "conformance suite"

    if parse_failures:
        parse_level = "error" if parse_failures_gate and not exit_zero else "warning"
        n = len(parse_failures)
        shown = parse_failures[:_MAX_PARSE_FILE_ANNOTATIONS]
        for failure in shown:
            print(
                f"::{parse_level} file={_pct_prop(failure.file)},"
                f"line={failure.line},col={failure.column},"
                f"title={_pct_prop('Conformance: file not scanned')}"
                f"::{_pct(failure.message)}"
            )
        more = (
            f" Only the first {len(shown)} are annotated; the log lists all {n}."
            if n > len(shown)
            else ""
        )
        msg = _pct(
            f"{n} file{'s' if n != 1 else ''} could not be parsed, so no check "
            f"in {label} scanned {'them' if n != 1 else 'it'}.{more} {detail}"
        )
        title = _pct_prop(
            f"Conformance: {n} file{'s' if n != 1 else ''} not scanned ({label})"
        )
        print(f"::{parse_level} title={title}::{msg}")

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


def _is_excluded(rel: str, excluded_prefixes: tuple[str, ...]) -> bool:
    """Whether repo-relative POSIX path *rel* falls under an ``--exclude`` prefix.

    Matched on path-component boundaries: ``tools`` covers ``tools`` and
    ``tools/x.py`` but never ``tools_extra/x.py``.
    """
    return any(
        rel == prefix or rel.startswith(prefix + "/") for prefix in excluded_prefixes
    )


def _drop_excluded(
    paths: Iterable[Path], root: Path, excluded_prefixes: tuple[str, ...]
) -> list[Path]:
    """Return *paths* minus those under an ``--exclude`` prefix of *root*."""
    if not excluded_prefixes:
        return list(paths)
    return [
        p
        for p in paths
        if not _is_excluded(p.relative_to(root).as_posix(), excluded_prefixes)
    ]


def _python_files(paths: Iterable[Path]) -> set[Path]:
    """The discovered paths that are Python sources (checks also discover YAML,
    Dockerfiles and the repo root itself)."""
    return {p for p in paths if p.suffix == ".py" and p.is_file()}


def _parse_failures(py_paths: Iterable[Path], root: Path) -> list[ParseFailure]:
    """Parse each discovered Python file once; return the ones that fail.

    Every AST check skips a file it cannot parse, and nearly all of them do it
    silently.  Checking once here, over the union of what the in-scope checks
    discovered, reports that gap for every check without each one having to.
    """
    failures = (parse_failure(p, root) for p in sorted(py_paths))
    return [f for f in failures if f is not None]


def _to_notification(failure: ParseFailure, *, gating: bool = True) -> Notification:
    """SARIF notification for *failure*; ``error`` gates the run, ``warning`` does not."""
    return Notification(
        message={"text": failure.message},
        level="error" if gating else "warning",
        descriptor={"id": PARSE_FAILURE_ID},
        locations=[
            Location(
                physical_location=PhysicalLocation(
                    artifact_location=ArtifactLocation(uri=failure.file),
                    region=Region(start_line=failure.line, start_column=failure.column),
                )
            )
        ],
    )


def _checks_for_rules(rule_ids: set[str]) -> list[CheckRegistration]:
    """Return the registered checks that can emit any of *rule_ids*."""
    series = {rid[0] for rid in rule_ids}
    active = [c for c in _CHECKS if c.series in series]
    if rule_ids <= {rule.id for rule in PREFLIGHT_RULES}:
        active = [c for c in active if c.scan_all is preflight.scan_all]
    return active


def _collect(
    root: Path,
    active: list[CheckRegistration],
    *,
    active_scope: RuleScope | None,
    excluded_prefixes: tuple[str, ...],
    rule_ids: set[str] | None,
    series: set[str] | None,
) -> tuple[list[Finding], set[Path]]:
    """Run *active* over *root*; return (in-scope findings, Python files seen).

    One tree, one pass. ``main`` calls it for the working tree, and ``--since``
    calls it again for the base tree with the rules that fired.
    """
    all_findings: list[Finding] = []
    py_paths: set[Path] = set()
    # One read, one parse and one discovery walk per file for the whole pass,
    # shared by every check (see _ast_common._source_cache). Scoped to this
    # call: the --since base pass scans a different tree with its own cache.
    with source_cache():
        for check in active:
            # Skip a whole check when none of its series' rules could be in
            # scope (e.g. the all-APP D-series when scanning the SDK itself).
            if not _series_in_scope(check.series, active_scope):
                continue
            paths = _drop_excluded(check.discover(root), root, excluded_prefixes)
            py_paths |= _python_files(paths)
            all_findings.extend(check.scan(paths, root))

    # --exclude also applies at the finding level: a check that discovers the
    # repo root (e.g. K029) reads files beneath it that _drop_excluded never
    # saw, so filtering paths alone would not keep the "all checks" promise.
    all_findings = [
        f for f in all_findings if not _is_excluded(f.file, excluded_prefixes)
    ]

    selected_rules = {
        rid
        for rid, rule in CATALOG.items()
        if _rule_in_scope(rule.scope, active_scope)
        and (rule_ids is None or rid in rule_ids)
        and (series is None or rid[0] in series)
    }
    if "F015" in selected_rules:
        all_findings.extend(
            finding
            for finding in scan_removed_config(root)
            if not _is_excluded(finding.file, excluded_prefixes)
        )
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
    return all_findings, py_paths


def _blocking(findings: list[Finding]) -> list[Finding]:
    """Return the unsuppressed BLOCK-tier findings: the ones that gate."""
    return [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.BLOCK
    ]


def _introduced_blocking(
    root: Path,
    since: str,
    findings: list[Finding],
    *,
    active_scope: RuleScope | None,
    excluded_prefixes: tuple[str, ...],
) -> list[Finding] | None:
    """Return the blocking findings the change since *since* adds.

    ``None`` when the base cannot be compared: the caller then keeps the
    whole-repo verdict, so an error only ever makes the gate stricter. See
    :mod:`conformance.suite.introduced` for how "adds" is decided.
    """
    head = _blocking(findings)
    fired = {f.rule_id for f in head}

    def scan_base(tree: Path) -> list[Finding]:
        base, _ = _collect(
            tree,
            _checks_for_rules(fired),
            active_scope=active_scope,
            excluded_prefixes=excluded_prefixes,
            rule_ids=fired,
            series=None,
        )
        return _blocking(base)

    try:
        adds, base_sha = introduced_since(root, since, head, scan_base)
    except BaseUnavailable as exc:
        print(
            f"conformance (--since {since}): {exc}; "
            "falling back to the whole-repository verdict.\n"
        )
        return None
    inherited = len(head) - len(adds)
    print(
        f"conformance (--since {since}, base {base_sha[:12]}): "
        f"{len(adds)} blocking introduced by this change, "
        f"{inherited} inherited from the base (reported, not gating)."
    )
    for f in adds:
        print(f"  [{f.rule_id}] [INTRODUCED] {f.file}:{f.line}:{f.column}")
    print()
    return adds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atlan conformance suite runner.")
    parser.add_argument("--repo", default=".", metavar="DIR")
    parser.add_argument(
        "--output", metavar="FILE", help="Write SARIF to FILE (default: stdout)"
    )
    parser.add_argument("--tool-version", default=TOOL_VERSION, metavar="VERSION")
    parser.add_argument(
        "--static",
        action="store_true",
        help="Static analysis only. Always the case; accepted for compatibility.",
    )
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
    parser.add_argument(
        "--since",
        metavar="REF",
        default=os.getenv(SINCE_ENV, ""),
        help=(
            "Gate on the change, not the repository: exit non-zero only for "
            "blocking findings the working tree has over the merge-base of REF "
            "and HEAD (e.g. 'origin/main'). Findings the base already carries "
            "are still reported, and still written to the SARIF, but do not "
            "fail the run. Warnings never fail it. A file that cannot be "
            "parsed still fails it, as without --since. If the base cannot "
            "be compared, every blocking finding fails. "
            f"Default: ${SINCE_ENV}."
        ),
    )
    args = parser.parse_args(argv)

    requested: set[str] = set()
    rule_ids: set[str] | None = None
    if args.rule:
        if args.series:
            parser.error(
                "--rule and --series are mutually exclusive (--rule implies its series)"
            )
        rule_ids = parse_rule_ids(args.rule)
        active = _checks_for_rules(rule_ids)
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

    all_findings, py_paths = _collect(
        root,
        active,
        active_scope=active_scope,
        excluded_prefixes=excluded_prefixes,
        rule_ids=rule_ids,
        series=requested if args.series else None,
    )

    parse_failures = _parse_failures(py_paths, root)
    # A --rule run's exit code is scoped to exactly the requested rules, but
    # py_paths spans every check in their series, and no mapping says which
    # check owns which rule. An unparseable file there may be one the requested
    # rules never scan, so it is reported as a warning and does not gate.
    parse_failures_gate = rule_ids is None

    # Always surface violations in a human-readable form so CI logs are actionable.
    _print_human_summary(
        all_findings,
        args.rule or args.series,
        excluded_prefixes,
        parse_failures,
        parse_failures_gate=parse_failures_gate,
    )

    # --since: which blocking findings did this change add? ``None`` means the
    # question was not asked (or could not be answered), and the whole-repo
    # verdict stands.
    change_adds: list[Finding] | None = None
    if args.since:
        change_adds = _introduced_blocking(
            root,
            args.since,
            all_findings,
            active_scope=active_scope,
            excluded_prefixes=excluded_prefixes,
        )
    # An unanalysed file fails the run whatever the change added: a clean
    # verdict must mean every discovered file was scanned. Parse failures
    # are not compared with the base.
    unanalysed = bool(parse_failures) and parse_failures_gate
    # True only when --since turns a whole-repo failure into a pass.
    inherited_only = change_adds is not None and not change_adds and not unanalysed

    if os.getenv("GITHUB_ACTIONS") == "true":
        _emit_github_summary_annotations(
            all_findings,
            args.rule or args.series,
            excluded_prefixes,
            # Inherited findings annotate as warnings: they do not fail the run.
            exit_zero=args.exit_zero or inherited_only,
            parse_failures=parse_failures,
            parse_failures_gate=parse_failures_gate,
        )

    report = findings_to_report(
        all_findings,
        tool_version=args.tool_version,
        excluded_paths=list(excluded_prefixes),
        rule_ids=rule_ids,
        notifications=[
            _to_notification(f, gating=parse_failures_gate) for f in parse_failures
        ],
    )
    for result in report.runs[0].results:
        if result.rule_id == "F019":
            result.properties["atlan/analysisStatus"] = "unresolved"
    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    sarif_exit_code: int = report.runs[0].invocations[0].exit_code  # type: ignore[assignment]
    if args.exit_zero:
        return 0
    if change_adds is None:
        return sarif_exit_code
    # The SARIF above keeps the whole-repo result; only the process exit code
    # answers the narrower question.
    return 1 if change_adds or unanalysed else 0


if __name__ == "__main__":
    sys.exit(main())

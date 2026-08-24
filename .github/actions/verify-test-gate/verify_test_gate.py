"""Evaluate the Tests Gate and render its status strings — one source of truth.

The aggregator gate is the single required check for branch protection. This
driver is both the pass/fail authority AND the source of the human-readable
status strings the PR "Tests Summary" table shows, so the displayed status and
the enforced result can never drift.

It is also the authority for the CROSS-REPO callback: tests-reusable's
report-to-sdk job completes the ``Connector E2E run / <app>`` check run on the
dispatching application-sdk PR using this driver's ``conclusion`` output. That
matters because it used to compute its own verdict from a strictly smaller set
of job results (no discover-e2e, no anomaly rule), so a connector run whose
Tests Gate was red could still report green on the SDK side — a failure in the
triggered app invisible to the PR that triggered it. One driver, two consumers,
no second implementation to drift.

Gate passes iff:

  * ``unit`` tests succeeded (the per-commit tier; runs on every event), AND
  * ``detect-integration`` (the suite-detection job that gates the integration
    tier) succeeded OR was skipped (skipped = the tier does not apply to this
    event; a *failed* detection must not be indistinguishable from "no suite" —
    otherwise a checkout/glob flake silently skips integration and greens the
    gate), AND
  * ``detect-merge-queue`` (which decides whether the integration tier runs in
    the merge queue or on the PR) succeeded OR was skipped (skipped = a non-PR
    event, where the question does not arise). Same reasoning as above: a
    *failed* detection drops the integration tier to a skip, so it must fail the
    gate rather than green it, AND
  * ``integration`` tests succeeded OR were skipped (skipped is legitimate: the
    job is skipped on pull_request when a merge queue will run it instead, and
    when the connector ships no integration suite), AND
  * e2e discovery succeeded OR was skipped (skipped = e2e not requested; a
    *failed* discovery means e2e was requested but no suites were found), AND
  * the per-arch e2e image build, the manifest merge, the tenant lease and the
    tenant install succeeded OR were skipped (skipped = the install path is off,
    or e2e was not requested), AND
  * the e2e matrix succeeded OR was skipped (matrix aggregate is success only
    if every leg passed).

Note the asymmetry with e2e: ``discover-e2e`` *fails* on count=0 (it only runs
when e2e was explicitly requested, so zero suites is a misconfig), which is why
``discover-e2e == success and e2e == skipped`` is an anomaly. ``detect-
integration`` *succeeds* on count=0 (a connector with no integration suite is
legitimate) and ``integration`` then skips cleanly, so there is no analogous
``detect-integration == success and integration == skipped`` check — that pair
is the normal no-suite path.

The install-path jobs are checked for the same reason the detection jobs are:
``build-e2e-image``, ``merge-e2e-image``, ``lease-tenant`` and ``prepare-tenant``
are exactly the jobs the e2e matrix's own ``if`` gates on, so a failure in any of them drops
the matrix to a skip. The anomaly rule above already refused to green that, but
it named the symptom ("matrix skipped") rather than the cause ("the arm64 image
build failed") — which is precisely what made the first real occurrence take a
reading of four workflow files to explain. Checking them directly puts the cause
in the annotation, and the anomaly rule then fires only when nothing upstream
explains the skip.

It emits GitHub Actions outputs (``passed`` + ``conclusion`` + per-row/overall
status strings) and, when failing, ``::error::`` annotations. It does NOT exit
non-zero — the gate job enforces via a branch-free
``if: ... outputs.passed != 'true'`` step, keeping conditional logic out of
inline shell per ``docs/standards/ci.md``.

Co-located with the composite action (checked out with it in consumer repos).
"""

from __future__ import annotations

import argparse
import sys

# Job results acceptable for the optional e2e path. ``skipped`` covers both
# "e2e not requested" and "discovery skipped"; anything else fails the gate.
_OK_OPTIONAL = ("success", "skipped")

# A job that never ran to a verdict. GitHub reports this for a manual cancel,
# for a run superseded via cancel-in-progress — and for a concurrency-group
# EVICTION, which is the one worth spelling out: GitHub keeps only ONE pending
# run per group, so a third arrival cancels the queued one before it is ever
# given a runner (no log output at all). None of these are test failures, and
# the fix for all of them is "re-run", not "triage the diff". The gate still
# BLOCKS — an un-run test cannot green a merge — but it must not report the
# result as though the code was found wanting. See FND-218.
_CANCELLED = "cancelled"

_CANCELLED_GUIDANCE = (
    "no test reported a verdict: the job(s) above were cancelled, not failed "
    "(manual cancel, superseded commit, or a concurrency-group eviction). "
    "Re-run rather than triage the diff."
)


def _install_path(
    build_e2e_image: str,
    merge_e2e_image: str,
    prepare_tenant: str,
    lease_tenant: str,
) -> list[tuple[str, str, str]]:
    """The jobs between discovery and the e2e matrix, in order.

    Each entry is (annotation phrase, summary-row phrase, result). One list so
    ``evaluate`` and ``_e2e_status`` cannot disagree about which jobs belong to
    this leg, about their order, or about which one a reader is pointed at when
    several fail together.

    ``lease-tenant`` sits where it runs: after the image exists and immediately
    before the install, because it is the job that waits for the tenant to be
    free (FND-250). Its failure mode is contention, not a broken change, so it
    has to be named — left unnamed it would surface only as the downstream
    "matrix skipped despite discovered suites" anomaly, which reads like a
    workflow misconfiguration.

    No parameter here has a default, deliberately: every caller must name every
    job. A default let a call site that predated ``lease_tenant`` keep compiling
    while silently computing the install path as though the lease had passed,
    which relabelled a cancelled lease as ``failure``. The public functions keep
    their trailing defaults — they are consumed cross-repo at ``@main`` and
    cannot break callers — but this helper is module-private, so a missed
    argument should be a TypeError.
    """
    return [
        ("the e2e image build", "e2e image build", build_e2e_image),
        (
            "the e2e image manifest merge",
            "e2e image manifest merge",
            merge_e2e_image,
        ),
        ("the tenant lease", "Tenant lease", lease_tenant),
        ("the tenant install", "Tenant install", prepare_tenant),
    ]


def stood_down(superseded: str) -> bool:
    """Did the run deliberately abandon e2e because its SDK commit is stale?

    The connector-side recheck (``e2e-dispatch-recheck``, FND-701) skips
    ``lease-tenant`` and the e2e legs when the application-sdk commit under test
    is no longer the head of the PR that dispatched the run. That produces the
    exact shape the matrix-skipped anomaly below exists to catch — a successful
    discovery with a skipped matrix and no install-path failure to explain it —
    so without an explanation the gate would red, and ``report-to-sdk`` would
    mirror ``conclusion=failure`` onto the dispatching SDK commit: "your change
    broke the connector" for a run that was deliberately stood down. Precisely
    the misattribution the cancelled/failure split exists to prevent (FND-218).

    Anything other than the literal "true" is False, and the anomaly still fires.
    A stand-down has to be positively asserted by the job that decided it; an
    absent, empty or unparseable value means the skip is still unexplained, which
    is the state worth reddening.
    """
    return superseded.strip().lower() == "true"


def evaluate(
    unit: str,
    integration: str,
    detect_integration: str,
    discover_e2e: str,
    e2e: str,
    detect_merge_queue: str = "skipped",
    build_e2e_image: str = "skipped",
    merge_e2e_image: str = "skipped",
    prepare_tenant: str = "skipped",
    lease_tenant: str = "skipped",
    superseded: str = "false",
) -> list[str]:
    """Return human-readable failure reasons (empty ⇒ the gate passes).

    ``detect_merge_queue`` and the four install-path results are trailing and
    default to "skipped" because this driver is consumed cross-repo via
    ``@main``: a required positional would break every caller the instant it
    merged, before their workflows could update. "skipped" is the neutral
    default in every case — it is what a caller that does not run the job at
    all would report.
    """
    errors: list[str] = []
    if unit != "success":
        errors.append(f"unit tests did not succeed (result={unit})")
    # detect-integration gates the integration job's `if`. A *failure* there
    # (checkout/glob flake) drops integration to a skip, which the check below
    # would read as a legitimate pass — so a failed detection must fail the gate
    # here, exactly as a failed discover-e2e does. skipped (pull_request) and
    # success (suite present or not) are both valid.
    if detect_integration not in _OK_OPTIONAL:
        errors.append(
            f"integration-suite detection did not succeed (result={detect_integration})"
        )
    # Same hole, one job upstream: detect-merge-queue decides whether the
    # integration tier runs on the PR at all, so a failure there also drops the
    # tier to a skip that the check below would read as a legitimate pass.
    if detect_merge_queue not in _OK_OPTIONAL:
        errors.append(
            f"merge-queue detection did not succeed (result={detect_merge_queue})"
        )
    # Integration is optional-by-skip: the job is intentionally skipped on
    # pull_request and when the connector has no integration suite. Any result
    # other than success/skipped (failure, cancelled, timed_out) fails the gate.
    if integration not in _OK_OPTIONAL:
        errors.append(f"integration tests did not succeed (result={integration})")
    if discover_e2e not in _OK_OPTIONAL:
        errors.append(f"e2e discovery did not succeed (result={discover_e2e})")
    # The install path, in order. These are exactly the jobs the e2e matrix's own
    # `if` gates on, so a failure in any of them silently drops the matrix to a
    # skip. Named directly so the annotation reports the cause rather than the
    # downstream symptom.
    for annotation, _row, result in _install_path(
        build_e2e_image, merge_e2e_image, prepare_tenant, lease_tenant
    ):
        if result not in _OK_OPTIONAL:
            errors.append(f"{annotation} did not succeed (result={result})")
    if e2e not in _OK_OPTIONAL:
        errors.append(f"one or more e2e suites did not succeed (result={e2e})")
    # Defensive: a successful discovery means suites exist (discover-e2e fails on
    # count=0), so the matrix should have run. A skipped matrix here is an
    # anomaly — this driver is consumed cross-repo via @main, so don't let a
    # future caller that re-wires the e2e `if` green the gate by skipping it.
    #
    # Suppressed when an install-path job already failed: that IS the explanation
    # for the skip, and it is reported above with the failing job named. Firing
    # both would bury the cause under the symptom.
    #
    # Suppressed for the same reason by a stand-down (FND-701): the run skipped
    # its own e2e because the SDK commit it was dispatched for is no longer the
    # head of its PR. That is an explained skip, and it is asserted by the job
    # that decided it rather than inferred from the shape of the results.
    if (
        not stood_down(superseded)
        and discover_e2e == "success"
        and e2e == "skipped"
        and all(
            result in _OK_OPTIONAL
            for _annotation, _row, result in _install_path(
                build_e2e_image, merge_e2e_image, prepare_tenant, lease_tenant
            )
        )
    ):
        errors.append(
            "e2e discovery succeeded (suites found) but the matrix was skipped"
        )
    return errors


def cancelled_only(
    unit: str,
    integration: str,
    detect_integration: str,
    discover_e2e: str,
    e2e: str,
    detect_merge_queue: str = "skipped",
    build_e2e_image: str = "skipped",
    merge_e2e_image: str = "skipped",
    prepare_tenant: str = "skipped",
    lease_tenant: str = "skipped",
    superseded: str = "false",
) -> bool:
    """True when at least one job was cancelled and nothing actually failed.

    Deliberately requires a cancellation to be *present* rather than merely an
    absence of failures. The "discovery succeeded but the matrix was skipped"
    anomaly produces an error while every result sits in ``_OK_OPTIONAL``;
    treating that as a cancellation would hide a real misconfiguration behind a
    "just re-run" verdict — the opposite of what this distinction is for. The
    anomaly is raised by ``evaluate`` independently of the raw job results, so a
    cancellation elsewhere does not make it disappear: it must be excluded
    explicitly, or a simultaneous cancellation would mask it.
    """
    # The matrix-skipped anomaly fires on raw results that all sit in
    # _OK_OPTIONAL, so it is invisible to the non_ok filter below. It is the one
    # gate error that is never cancellation-attributable, so its presence means
    # the block is not a pure cancellation — spell it "failure" and surface the
    # misconfiguration, never "just re-run".
    #
    # A stand-down is exempt for the same reason it is exempt above: it is not an
    # anomaly at all, so it must not force the "never a pure cancellation" answer
    # and relabel a genuinely cancelled run as a failure.
    if (
        not stood_down(superseded)
        and discover_e2e == "success"
        and e2e == "skipped"
        and all(
            result in _OK_OPTIONAL
            for _annotation, _row, result in _install_path(
                build_e2e_image, merge_e2e_image, prepare_tenant, lease_tenant
            )
        )
    ):
        return False
    non_ok = [
        result
        for result in (
            unit,
            integration,
            detect_integration,
            discover_e2e,
            e2e,
            detect_merge_queue,
            build_e2e_image,
            merge_e2e_image,
            prepare_tenant,
            lease_tenant,
        )
        if result not in _OK_OPTIONAL
    ]
    return bool(non_ok) and all(result == _CANCELLED for result in non_ok)


# Row text for a job that was cancelled. Distinct glyph from ❌ on purpose: the
# summary table is the first thing read, and "failed" there sends a reviewer
# into a diff that is not the problem.
_CANCELLED_ROW = "🚫 Cancelled — not run"


def _unit_status(unit: str) -> str:
    if unit == "success":
        return "✅ Passed"
    if unit == "skipped":
        return "⊘ Skipped"
    if unit == _CANCELLED:
        return _CANCELLED_ROW
    return "❌ Failed"


def _integration_status(
    integration: str, detect_integration: str, detect_merge_queue: str = "skipped"
) -> str:
    # A detection failure drops integration to a skip; surface that as a failure
    # rather than the benign "skipped" string, so the display never claims the
    # tier was cleanly skipped when detection actually broke.
    if detect_merge_queue == _CANCELLED:
        return "🚫 Merge-queue detection cancelled — not run"
    if detect_merge_queue not in _OK_OPTIONAL:
        return "❌ Merge-queue detection failed"
    if detect_integration == _CANCELLED:
        return "🚫 Integration-suite detection cancelled — not run"
    if detect_integration not in _OK_OPTIONAL:
        return "❌ Integration-suite detection failed"
    if integration == "success":
        return "✅ Passed"
    if integration == _CANCELLED:
        return _CANCELLED_ROW
    if integration == "skipped":
        # Two distinct reasons, and the difference matters to a reader deciding
        # whether their change was actually exercised: a queue will run the tier
        # on the batched merge, whereas "no suite" means it runs nowhere.
        return "⊘ Skipped — no integration suite, or it runs in the merge queue"
    return "❌ Failed"


def _e2e_status(
    discover_e2e: str,
    e2e: str,
    build_e2e_image: str = "skipped",
    merge_e2e_image: str = "skipped",
    prepare_tenant: str = "skipped",
    lease_tenant: str = "skipped",
    superseded: str = "false",
) -> str:
    if discover_e2e == "skipped":
        return "⊘ Skipped — add `e2e` label to trigger"
    if discover_e2e == _CANCELLED:
        return "🚫 e2e discovery cancelled — not run"
    if discover_e2e == "failure":
        return "❌ No suites discovered (e2e was requested)"
    # Ahead of the success/skip checks: an install-path failure is WHY the matrix
    # skipped, and the row must say so rather than report a benign skip. Reported
    # in the same order and with the same labels as the annotations, from the one
    # list, so the row and the error text always name the same job.
    for _annotation, row, result in _install_path(
        build_e2e_image, merge_e2e_image, prepare_tenant, lease_tenant
    ):
        if result == _CANCELLED:
            return f"🚫 {row} cancelled — not run"
        if result not in _OK_OPTIONAL:
            # The lease row says why, because "failed" alone would send a reader
            # into their own diff: the tenant was busy, not broken.
            if row == "Tenant lease":
                return "⏳ Tenant busy — lease not acquired, re-run"
            return f"❌ {row} failed"
    if e2e == "success":
        return "✅ Passed"
    # Before the anomaly row, and mirroring the order in `evaluate`: a stand-down
    # produces exactly the anomaly's shape, so whichever check runs first decides
    # what the reader is told. "Matrix skipped despite discovered suites" would
    # send them looking for a workflow misconfiguration that is not there.
    if stood_down(superseded) and e2e == "skipped":
        return "⊘ Stood down — superseded SDK commit"
    if discover_e2e == "success" and e2e == "skipped":
        return "❌ Matrix skipped despite discovered suites"
    if e2e == "skipped":
        return "⊘ Skipped"
    if e2e == _CANCELLED:
        return _CANCELLED_ROW
    return "❌ Failed"


def render(
    unit: str,
    integration: str,
    detect_integration: str,
    discover_e2e: str,
    e2e: str,
    detect_merge_queue: str = "skipped",
    build_e2e_image: str = "skipped",
    merge_e2e_image: str = "skipped",
    prepare_tenant: str = "skipped",
    lease_tenant: str = "skipped",
    superseded: str = "false",
) -> dict[str, str]:
    """Compute the gate's outputs: pass/fail + the display status strings.

    ``conclusion`` is the same verdict as ``passed``, spelled as a GitHub Checks
    API conclusion. It exists so the cross-repo callback can wire this driver
    straight into ``complete_check_run.py --conclusion`` — mapping true/false to
    success/failure in workflow YAML would put the decision back outside the one
    tested authority, which is the drift this driver exists to prevent.

    A blocked gate is spelled ``cancelled`` rather than ``failure`` when nothing
    actually failed and the blocking results are all cancellations. ``passed``
    is "false" either way — the gate must never green an un-run test — but the
    two are NOT interchangeable to a reader: a mirrored ``failure`` on the
    dispatching SDK PR reads as "your change broke the connector" and sends
    someone into the wrong diff, which is precisely what happened in FND-218.
    ``cancelled`` is a valid Checks API conclusion and has been in
    ``complete_check_run.py``'s accepted set since that script was introduced,
    so this is safe for the oldest dispatching SDK ref that can still call back.
    """
    errors = evaluate(
        unit,
        integration,
        detect_integration,
        discover_e2e,
        e2e,
        detect_merge_queue,
        build_e2e_image,
        merge_e2e_image,
        prepare_tenant,
        lease_tenant,
        superseded,
    )
    blocked_by_cancellation = errors and cancelled_only(
        unit,
        integration,
        detect_integration,
        discover_e2e,
        e2e,
        detect_merge_queue,
        build_e2e_image,
        merge_e2e_image,
        prepare_tenant,
        lease_tenant,
        superseded,
    )
    return {
        "passed": "true" if not errors else "false",
        "conclusion": (
            "success"
            if not errors
            else _CANCELLED
            if blocked_by_cancellation
            else "failure"
        ),
        "unit-status": _unit_status(unit),
        "integration-status": _integration_status(
            integration, detect_integration, detect_merge_queue
        ),
        "e2e-status": _e2e_status(
            discover_e2e,
            e2e,
            build_e2e_image,
            merge_e2e_image,
            prepare_tenant,
            lease_tenant,
            superseded,
        ),
        "overall-status": (
            "✅ All passed"
            if not errors
            else "🚫 Cancelled — no verdict, re-run"
            if blocked_by_cancellation
            else "❌ Some failed"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate + render the Tests Gate.")
    parser.add_argument("--unit", required=True, help="needs.unit.result")
    parser.add_argument("--integration", required=True, help="needs.integration.result")
    parser.add_argument(
        "--detect-integration",
        required=True,
        help="needs.detect-integration.result",
    )
    # Optional with a "skipped" default: this driver is consumed cross-repo at
    # @main, so a required flag would break callers that have not yet wired the
    # detect-merge-queue job.
    parser.add_argument(
        "--detect-merge-queue",
        default="skipped",
        help="needs.detect-merge-queue.result",
    )
    parser.add_argument(
        "--discover-e2e", required=True, help="needs.discover-e2e.result"
    )
    # Optional for the same cross-repo reason as --detect-merge-queue above.
    parser.add_argument(
        "--build-e2e-image",
        default="skipped",
        help="needs.build-e2e-image.result",
    )
    parser.add_argument(
        "--merge-e2e-image",
        default="skipped",
        help="needs.merge-e2e-image.result",
    )
    parser.add_argument(
        "--lease-tenant",
        default="skipped",
        help="needs.lease-tenant.result (queues for the shared tenant)",
    )
    parser.add_argument(
        "--prepare-tenant",
        default="skipped",
        help="needs.prepare-tenant.result",
    )
    parser.add_argument("--e2e", required=True, help="needs.e2e.result")
    parser.add_argument(
        "--superseded",
        default="false",
        help='needs.sdk-head-recheck.outputs.superseded. "true" explains a '
        "skipped e2e matrix as a deliberate stand-down rather than the "
        "misconfiguration the matrix-skipped anomaly exists to catch. Optional "
        'and defaulting to "false" so callers pinned at @main that have not '
        "wired the job keep the previous behaviour.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Annotate each failure reason (shows on the gate step regardless of the
    # zero exit; the job's enforce step turns `passed=false` into a red check).
    for reason in evaluate(
        args.unit,
        args.integration,
        args.detect_integration,
        args.discover_e2e,
        args.e2e,
        args.detect_merge_queue,
        args.build_e2e_image,
        args.merge_e2e_image,
        args.prepare_tenant,
        args.lease_tenant,
        args.superseded,
    ):
        print(f"::error::{reason}", file=sys.stderr)

    # Follows the per-job reasons so the annotation list reads cause-then-verdict:
    # which jobs did not report, then what that means for whoever is looking.
    if cancelled_only(
        args.unit,
        args.integration,
        args.detect_integration,
        args.discover_e2e,
        args.e2e,
        args.detect_merge_queue,
        args.build_e2e_image,
        args.merge_e2e_image,
        args.prepare_tenant,
        args.lease_tenant,
        args.superseded,
    ):
        print(f"::error::{_CANCELLED_GUIDANCE}", file=sys.stderr)

    for key, value in render(
        args.unit,
        args.integration,
        args.detect_integration,
        args.discover_e2e,
        args.e2e,
        args.detect_merge_queue,
        args.build_e2e_image,
        args.merge_e2e_image,
        args.prepare_tenant,
        args.lease_tenant,
        args.superseded,
    ).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

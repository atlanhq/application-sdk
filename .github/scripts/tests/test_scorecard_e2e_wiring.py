"""Guards for the FND-33 / FND-34 scorecard wiring in tests-reusable.yaml.

What is being protected is that **absence stays absence**. Every failure mode
here is silent: a scorecard that scored a tier nobody ran, or recorded cross-CSP
coverage nobody exercised, is a well-formed document that publishes cleanly and
tells the fleet dashboard something untrue. Nothing goes red.

Deliberately YAML-shape assertions: the wiring is GitHub Actions' own (job
`needs`, skipped-job semantics, artifact download modes) and cannot be exercised
without a runner. The scoring logic is unit-tested in the conformance package's
test_scorecard_* modules, and the argument-selection logic in
test_build_scorecard_args.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"

_DOWNLOAD = "actions/download-artifact@"
_ARGS_SCRIPT = "build_scorecard_args.py"


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


@pytest.fixture(scope="module")
def scorecard(jobs: dict[str, Any]) -> dict[str, Any]:
    return jobs["scorecard"]


def _steps_using(job: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith(prefix)]


# ── The evidence has to be reachable at all ──────────────────────────────────


def test_the_scorecard_needs_the_e2e_jobs(scorecard: dict[str, Any]) -> None:
    # Without these in `needs`, `needs.e2e.result` and the discovered cloud list
    # are simply absent, and the e2e tier is not-applicable on EVERY run of
    # EVERY app — which is the state FND-33 exists to end.
    assert {"discover-e2e", "e2e"} <= set(scorecard["needs"])


def test_a_skipped_e2e_does_not_skip_the_scorecard(scorecard: dict[str, Any]) -> None:
    """`needs` is gated separately from `if:`, and a skipped need fails it.

    e2e is skipped on the routine push — the overwhelmingly common case — so
    losing `always()` here would stop emitting scorecards fleet-wide rather
    than emit one without e2e.
    """
    assert scorecard["if"].startswith("always()")


def test_the_scorecard_still_never_runs_on_pull_request(
    scorecard: dict[str, Any],
) -> None:
    # On a PR the integration job is skipped, so a scorecard would publish a
    # zeroed integration tier: a fabricated regression. This is also why letting
    # the scorecard ride the `e2e`-labelled PR path was rejected as the answer to
    # the trigger-overlap question.
    assert "github.event_name != 'pull_request'" in scorecard["if"]


# ── Per-leg junits must not be merged into one ───────────────────────────────


def _e2e_download(job: dict[str, Any]) -> dict[str, Any]:
    matching = [
        s
        for s in _steps_using(job, _DOWNLOAD)
        if "sdr-integration-tests" in str(s.get("with", {}).get("pattern", ""))
    ]
    assert len(matching) == 1, "expected exactly one e2e evidence download"
    return matching[0]


def test_e2e_evidence_is_downloaded_by_pattern_not_exact_name(
    scorecard: dict[str, Any],
) -> None:
    # Since FND-6 each leg's artifact is suffixed with the leg name (which now
    # includes the cloud), so the historical exact name matches nothing and the
    # download quietly no-ops under continue-on-error.
    with_ = _e2e_download(scorecard)["with"]
    assert with_["pattern"].endswith("*-results")
    assert "name" not in with_


def test_e2e_legs_are_kept_apart_on_disk(scorecard: dict[str, Any]) -> None:
    """merge-multiple would have the legs overwrite each other.

    Every leg carries its junit at the SAME inner path
    (`results/sdr-test-results.xml`), so flattening leaves ONE file and the
    scorecard scores a single arbitrary leg as if it were the whole run.
    """
    assert _e2e_download(scorecard)["with"].get("merge-multiple") is not True


def test_the_e2e_download_is_best_effort(scorecard: dict[str, Any]) -> None:
    # No artifacts (e2e did not run) must leave the tier not-applicable, not
    # fail a reporting job.
    assert _e2e_download(scorecard)["continue-on-error"] is True


def test_the_callback_download_matches_the_per_leg_names_too(
    jobs: dict[str, Any],
) -> None:
    # report-to-sdk renders the SDK-side check body from this artifact; it hit
    # the same FND-6 rename and fell back to a plain body on every cross-CSP run.
    # merge-multiple is correct there and wrong above: that job wants exactly one
    # rendered pr-comment-body.md, this one wants every junit kept apart.
    download = [
        s
        for s in _steps_using(jobs["report-to-sdk"], _DOWNLOAD)
        if "sdr-integration-tests" in str(s.get("with", {}))
    ]
    assert len(download) == 1
    assert download[0]["with"]["pattern"].endswith("*-results")


# ── Argument selection stays in the tested script ────────────────────────────


def _generate_step(job: dict[str, Any]) -> dict[str, Any]:
    matching = [s for s in job["steps"] if _ARGS_SCRIPT in str(s.get("run", ""))]
    assert len(matching) == 1, (
        "the scorecard CLI's conditional arguments must come from the tested "
        f"{_ARGS_SCRIPT}, not from inlined shell — branching in YAML cannot be "
        "regression-tested (docs/standards/ci.md)"
    )
    return matching[0]


def test_the_generate_step_passes_the_job_results_the_script_needs(
    scorecard: dict[str, Any],
) -> None:
    env = _generate_step(scorecard)["env"]
    assert env["E2E_RESULT"] == "${{ needs.e2e.result }}"
    assert env["OBSERVED_CLOUDS"] == "${{ needs.discover-e2e.outputs.clouds }}"
    assert env["CONFIGURED_KNOWN"] == (
        "${{ steps.configured-clouds.outcome == 'success' }}"
    )
    assert env["CONFIGURED_CLOUDS"] == "${{ steps.configured-clouds.outputs.clouds }}"


def test_the_e2e_glob_is_not_expanded_by_the_shell(scorecard: dict[str, Any]) -> None:
    # The CLI expands it. Expanding it in the shell would need a loop this file
    # is not allowed to contain, and a bash glob that matches nothing expands to
    # the literal pattern — which the CLI would then read as a missing file.
    step = _generate_step(scorecard)
    assert "*" in step["env"]["E2E_JUNIT_GLOB"]
    assert step["env"]["E2E_JUNIT_GLOB"] not in step["run"]


def test_the_scorecard_never_fails_the_run(scorecard: dict[str, Any]) -> None:
    # Reporting, not a gate: an older published conformance that does not yet
    # understand the new flags must warn, never redden a merge-queue entry.
    assert "::warning::" in _generate_step(scorecard)["run"]


# ── `configured` is the field that must land on EVERY emission ───────────────


def _configured_step(job: dict[str, Any]) -> dict[str, Any]:
    matching = [s for s in job["steps"] if s.get("id") == "configured-clouds"]
    assert len(matching) == 1
    return matching[0]


def test_configured_is_resolved_without_needing_an_e2e_run(
    scorecard: dict[str, Any],
) -> None:
    """The rollout signal only works if it does not depend on e2e running.

    The scorecard runs on push/merge_group and e2e on label/dispatch, so a
    `configured` gated on the e2e job would be exactly as sparse as `observed`
    and FND-34 would deliver nothing on the routine path.
    """
    step = _configured_step(scorecard)
    assert "needs.e2e" not in str(step.get("if", ""))
    assert "needs.discover-e2e" not in str(step.get("if", ""))


def test_configured_uses_the_same_driver_and_narrowing_as_discovery(
    scorecard: dict[str, Any], jobs: dict[str, Any]
) -> None:
    # Re-deriving the list independently would let the recorded rollout state
    # drift from the fan-out that actually happens.
    step = _configured_step(scorecard)
    assert "discover_e2e_suites.py" in step["run"]
    assert "--clouds-only" in step["run"]
    assert (
        step["env"]["AVAILABLE_CLOUDS"] == "${{ steps.matrix-clouds.outputs.clouds }}"
    )

    discovery_clouds = next(
        s["with"]["clouds"]
        for s in jobs["discover-e2e"]["steps"]
        if s.get("id") == "discover"
    )
    assert step["env"]["CLOUDS"] == discovery_clouds, (
        "the `clouds` expression must be identical to discovery's, or the "
        "recorded configuration is not the configuration that would run"
    )


def test_resolving_configured_can_never_redden_the_job(
    scorecard: dict[str, Any],
) -> None:
    # The driver exits non-zero when narrowing would leave no clouds at all; the
    # step's `outcome` is then the "not known" signal, not a failure.
    assert _configured_step(scorecard)["continue-on-error"] is True


def test_the_tenant_matrix_blob_never_leaves_the_keys_step(
    scorecard: dict[str, Any],
) -> None:
    # The KEY LIST crosses this boundary; the credentials do not. Same posture as
    # discover-e2e, pinned by test_e2e_cloud_narrowing_wiring.py there.
    keys_steps = [s for s in scorecard["steps"] if s.get("id") == "matrix-clouds"]
    assert len(keys_steps) == 1
    assert keys_steps[0]["env"]["E2E_TENANT_MATRIX_JSON"] == (
        "${{ secrets.E2E_TENANT_MATRIX_JSON }}"
    )
    for step in scorecard["steps"]:
        if step.get("id") == "matrix-clouds":
            continue
        assert "secrets.E2E_TENANT_MATRIX_JSON }}" not in str(step.get("env", {})), (
            f"`{step.get('name', step.get('id'))}` interpolates the tenant "
            "matrix's VALUE; only its cloud key list may leave that step"
        )


def test_the_sdk_scripts_checkout_is_pinned_to_this_workflows_sha(
    scorecard: dict[str, Any],
) -> None:
    # Both executed payloads (the arg builder and the discovery driver) come from
    # the workflow's own SHA, so a PR that changes them self-tests on its branch
    # rather than waiting for main.
    checkouts = [
        s
        for s in _steps_using(scorecard, "actions/checkout@")
        if s.get("with", {}).get("repository") == "atlanhq/application-sdk"
    ]
    assert len(checkouts) == 1
    with_ = checkouts[0]["with"]
    assert with_["ref"] == "${{ job.workflow_sha }}"
    assert with_["persist-credentials"] is False
    sparse = with_["sparse-checkout"]
    assert ".github/scripts" in sparse
    assert ".github/actions/discover-e2e-suites" in sparse


def test_discover_e2e_exposes_the_resolved_cloud_list(jobs: dict[str, Any]) -> None:
    assert (
        jobs["discover-e2e"]["outputs"]["clouds"]
        == "${{ steps.discover.outputs.clouds }}"
    ), "the observed cloud list must come from the matrix-building call itself"

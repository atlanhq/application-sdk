"""Guards for the prepare-tenant wiring in tests-reusable.yaml (FND-31).

Every assertion here protects something whose failure mode is a *silent wrong
version* rather than a red build — which is the entire problem FND-31 exists to
fix, so a regression that reintroduces it must not be able to pass CI.

Deliberately YAML-shape assertions: the behaviour being protected is GitHub
Actions' own (job-result propagation, matrix output semantics, concurrency
grouping) and cannot be exercised without a runner.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import e2e_tenant_app as app  # noqa: E402
from _gha_expr import evaluate, evaluate_operand  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"
_SDR_ACTION = _REPO_ROOT / ".github/actions/sdr-e2e/action.yaml"

#: How much job budget the script's own two waits may account for. At 1/2, both
#: waits can run to completion and the rest of the job still has as long again —
#: so the runner's timeout is never the first thing to fire, whatever the checkout,
#: publish and read-back cost on a slow runner. A ratio rather than a fixed margin
#: because it stays correct if a wait's default changes.
_MAX_WAIT_SHARE = 0.5


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:  # type: ignore[type-arg]
    return workflow["jobs"]


# ── On by default, with a working opt-out ────────────────────────────────────


def test_install_is_on_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    # Flipped by FND-128. This assertion was `is False`, guarding the opposite
    # property: the install could not resolve a tenant without
    # E2E_TENANT_MATRIX_JSON, which was shared with a handful of repos, so
    # switching it on under the fleet would have reded every e2e leg.
    #
    # The secret is now org-wide, which removed that prerequisite and left the
    # opt-in doing harm instead: an un-adopted repo still fanned out across every
    # cloud in the matrix and tested whatever version each tenant already served.
    #
    # Kept as an assertion rather than deleted, pointing the other way: a silent
    # revert to opt-in would restore three-clouds-of-wrong-version green, which
    # no other test in this file would notice.
    #
    # `workflow[True]`: YAML 1.1 resolves the bare key `on` to the boolean true,
    # so safe_load never yields the string "on". Both spellings are accepted here
    # so the guard survives a future quoted `"on":`.
    triggers = workflow.get("on", workflow.get(True))
    spec = triggers["workflow_call"]["inputs"]["install-app-to-tenant"]
    assert spec["default"] is True
    assert spec["type"] == "boolean"


@pytest.mark.parametrize("job", ["build-e2e-image", "lease-tenant", "prepare-tenant"])
def test_new_jobs_are_gated_on_the_input(jobs: dict, job: str) -> None:  # type: ignore[type-arg]
    # Still gated, and it matters more now that the default is on: the gate is
    # what makes `install-app-to-tenant: false` a real opt-out for an app that
    # cannot be installed onto the e2e tenants, rather than a flag that reds the
    # run anyway.
    assert "inputs.install-app-to-tenant" in jobs[job]["if"], (
        f"{job} must not run when install-app-to-tenant is off — otherwise a repo "
        "that opted out still pays for a build and an install it declined"
    )


# ── A failed prepare-tenant must STOP the legs ───────────────────────────────


def test_e2e_needs_prepare_tenant(jobs: dict) -> None:  # type: ignore[type-arg]
    needs = jobs["e2e"]["needs"]
    assert "prepare-tenant" in needs and "build-e2e-image" in needs


def test_e2e_tolerates_skipped_but_not_failed_prepare(jobs: dict) -> None:  # type: ignore[type-arg]
    """The e2e legs must run when the new jobs are SKIPPED, and not when FAILED.

    Both halves are load-bearing and it is easy to get exactly one of them:

    * A job's `needs` gate is evaluated separately from its `if:`. With no
      status-check function, GitHub applies an implicit success() over every
      `needs` entry, a skipped need does not satisfy it, and the job is skipped
      before the `if:` is consulted. So the explicit `== 'skipped'` clauses are
      dead without a status-check override, and the legs disappear on the default
      path — which is every existing caller.
    * A bare `always()` with no result gates swings the other way and runs the
      legs after a FAILED install, testing whatever version the tenant happens to
      run.

    The correct shape is `always() && <explicit success-or-skipped gates>`, which
    is what `connector-tests` in pull_request.yaml already does over its own
    skippable need.

    An earlier version of this guard asserted `"always()" not in condition` — it
    codified the first bug and forbade the fix. Found by @sdk-review on #3023.
    """
    condition = " ".join(jobs["e2e"]["if"].split())

    # 1. A status-check function must override the implicit success()-over-needs.
    has_override = "always()" in condition or "!cancelled()" in condition
    assert has_override, (
        "the e2e `if:` has no status-check function, so GitHub's implicit "
        "success() over `needs` skips the job whenever build-e2e-image or "
        "prepare-tenant is skipped — which is the default path for every "
        "existing caller. The `== 'skipped'` clauses below are dead without it."
    )

    # 2. The explicit gates must still be there, or the override runs the legs on
    #    a FAILED install.
    #
    #    merge-e2e-image is NOT sufficient on its own: a failed arch leg leaves it
    #    SKIPPED (its own `if:` is unsatisfied), and 'skipped' is the benign value
    #    below — so the legs would run after a failed build, each building its own
    #    single-arch image against a tenant prepare-tenant never updated, with
    #    `expected-app-version` empty so the version check self-skips. Every job in
    #    the chain has to be named, not just the last one.
    for job in ("prepare-tenant", "build-e2e-image", "merge-e2e-image"):
        for result in ("success", "skipped"):
            expected = f"needs.{job}.result == '{result}'"
            assert expected in condition, (
                f"missing `{expected}`. With a status-check override present, "
                "these gates are the only thing stopping the legs from running "
                f"after a FAILED {job}."
            )


# ── The matrix-output trap ───────────────────────────────────────────────────


#: The two matrix jobs whose outputs are last-writer-wins with genuinely
#: different values per leg, so nothing may read a version or image off them.
_MATRIX_JOBS = ("prepare-tenant", "build-e2e-image")


@pytest.mark.parametrize("key", ["expected-app-version", "prebuilt-image"])
def test_the_legs_read_the_image_from_the_non_matrix_job(jobs: dict, key: str) -> None:  # type: ignore[type-arg]
    """Both values must come from merge-e2e-image, the only non-matrix source.

    Matrix job outputs are last-writer-wins in GitHub Actions. `prepare-tenant`
    fans out over clouds and `build-e2e-image` over architectures, and in both
    cases the legs write DIFFERENT values — so reading either would silently take
    whichever finished last (a per-arch image reference, or one cloud's view).
    merge-e2e-image is a single job producing the merged manifest, which is what
    every leg wants.
    """
    value = _sdr_step(jobs)["with"][key]
    assert "needs.merge-e2e-image.outputs." in value
    for job in _MATRIX_JOBS:
        assert job not in value, (
            f"{key} reads {job}, a matrix job whose outputs are "
            "last-writer-wins with different values per leg"
        )


def _sdr_step(jobs: dict) -> dict:  # type: ignore[type-arg]
    for step in jobs["e2e"]["steps"]:
        if "sdr-e2e" in str(step.get("uses", "")):
            return step
    raise AssertionError("the e2e job no longer invokes the sdr-e2e action")


# ── Tenant-scoped concurrency ────────────────────────────────────────────────


def test_prepare_tenant_leaves_tenant_exclusion_to_the_lease(jobs: dict) -> None:  # type: ignore[type-arg]
    """Tenant exclusion moved from this group to the (app, cloud) lease (FND-250).

    A tenant-keyed group here would now be actively harmful, not merely
    redundant: it holds ONE pending run, so a third run whose lease has not come
    up yet would be evicted before it ever got a runner — reintroducing FND-218
    inside the fix for it. Run-unique is the requirement, and it is only safe
    *because* lease-tenant serialises the tenant properly.
    """
    concurrency = jobs["prepare-tenant"]["concurrency"]
    group = concurrency["group"]

    assert "github.run_id" in group, (
        "prepare-tenant's group must be run-unique; anything shared across runs "
        "queues one-deep and evicts the rest (FND-218)"
    )
    assert "inputs.app-name" not in group, (
        "a tenant-keyed group here re-adds a one-pending-slot waiting room in "
        "front of the lease; serialisation belongs to lease-tenant"
    )
    assert "github.ref" not in group
    # Cancelling mid-install abandons the deployment and leaves the tenant in
    # whatever state LM reached.
    assert concurrency["cancel-in-progress"] is False


# ── The (app, cloud) tenant lease (FND-250) ──────────────────────────────────

_LEASE_ACTION = "atlanhq/application-sdk/.github/actions/e2e-tenant-lease@main"


def _lease_step(jobs: dict, job: str) -> dict:  # type: ignore[type-arg]
    for step in jobs[job]["steps"]:
        if "e2e-tenant-lease" in str(step.get("uses", "")):
            return step
    raise AssertionError(f"{job} no longer invokes the e2e-tenant-lease action")


@pytest.mark.parametrize(
    ("job", "mode"), [("lease-tenant", "acquire"), ("release-tenant", "release")]
)
def test_the_lease_jobs_use_the_shared_action_at_main(
    jobs: dict,  # type: ignore[type-arg]
    job: str,
    mode: str,
) -> None:
    """Pinned @main on purpose: every contender has to agree on the ref layout
    and the ordering rule, so the protocol must not vary by checked-out ref."""
    step = _lease_step(jobs, job)
    assert step["uses"] == _LEASE_ACTION
    assert step["with"]["mode"] == mode


def test_the_release_keys_on_app_and_cloud(jobs: dict) -> None:  # type: ignore[type-arg]
    """Acquire and release must name the same tenants, or every run leaks its
    ticket and the next contender waits for a holder that has already gone."""
    with_ = _lease_step(jobs, "release-tenant")["with"]
    assert with_["app"] == "${{ inputs.app-name }}"
    assert with_["cloud"] == "${{ matrix.cloud }}"


def test_the_acquire_is_one_job_over_an_ordered_cloud_set(jobs: dict) -> None:  # type: ignore[type-arg]
    """The deadlock fix, asserted as shape (FND-646).

    A per-cloud matrix acquires in PARALLEL and holds each lease while blocking
    on the rest — hold-and-wait. It deadlocked the first time two runs queued
    behind one holder: they raced for the freed set, split it, and each blocked on
    what the other held for the whole wait budget. With two or more runs queued
    that split is the expected outcome, so the parallel shape must not come back.
    """
    lease = jobs["lease-tenant"]
    assert "strategy" not in lease, (
        "lease-tenant must acquire every cloud in ONE job, in order; a matrix "
        "acquires them in parallel and reintroduces hold-and-wait (FND-646)"
    )
    with_ = _lease_step(jobs, "lease-tenant")["with"]
    assert with_["app"] == "${{ inputs.app-name }}"
    assert with_["clouds"] == "${{ needs.discover-e2e.outputs.cloud-list }}"
    assert "cloud" not in with_, (
        "passing both cloud and clouds fails the driver rather than silently "
        "preferring one; the acquire takes the set"
    )


@pytest.mark.parametrize("job", ["lease-tenant", "release-tenant"])
def test_the_lease_jobs_can_write_refs_and_read_runs(jobs: dict, job: str) -> None:  # type: ignore[type-arg]
    # contents: write creates and deletes the ticket; actions: read tells a live
    # holder from a dead one. Scoped to these two jobs so the long-running legs
    # that execute test code keep read-only contents.
    permissions = jobs[job]["permissions"]
    assert permissions["contents"] == "write"
    assert permissions["actions"] == "read"


def test_the_legs_do_not_get_ref_write(jobs: dict) -> None:  # type: ignore[type-arg]
    # Least privilege: the lease's write capability must not leak into the job
    # that runs connector test code.
    assert jobs["e2e"]["permissions"]["contents"] == "read"


def test_prepare_tenant_is_gated_on_the_lease(jobs: dict) -> None:  # type: ignore[type-arg]
    # Installing without the lease is precisely the race the lease closes, so
    # this is a gate and not merely an ordering edge.
    assert "lease-tenant" in jobs["prepare-tenant"]["needs"]
    assert "needs.lease-tenant.result != 'skipped'" in jobs["prepare-tenant"]["if"]


def test_prepare_tenant_confirms_its_own_clouds_lease_before_installing(
    jobs: dict,  # type: ignore[type-arg]
) -> None:
    """The job's `if:` can only see the lease matrix AGGREGATE, so it cannot tell
    "my cloud's lease succeeded" from "some cloud's did". Observed live: one
    cloud's acquire failed on a transient TLS error and the aggregate skipped the
    install for the two clouds whose leases HAD been taken — a run holding two
    tenants that installed onto neither.

    So the gate is widened to "the lease job ran" and each leg confirms its own
    tenant. That verify step must come FIRST, before anything touches the tenant.
    """
    steps = jobs["prepare-tenant"]["steps"]
    verify_at = _index_of(steps, "e2e_tenant_lease.py")
    assert verify_at is not None, (
        "prepare-tenant no longer confirms it holds its cloud's lease, so the "
        "install's precondition is back to the matrix aggregate"
    )
    assert "--mode verify" in steps[verify_at]["run"]
    assert steps[verify_at]["env"]["CLOUD"] == "${{ matrix.cloud }}"
    assert steps[verify_at]["env"]["APP"] == "${{ inputs.app-name }}"

    # Every step before it must be inert with respect to the tenant. Checkouts
    # qualify; anything that resolves credentials, publishes or installs does not.
    for step in steps[:verify_at]:
        assert "checkout" in str(step.get("uses", "")), (
            f"{step.get('name') or step.get('uses')} runs before the lease is "
            "confirmed; only steps that cannot touch the tenant may precede it"
        )


def test_prepare_tenant_verifies_with_its_own_driver_not_mains(jobs: dict) -> None:  # type: ignore[type-arg]
    """`uses:` cannot take an expression, so an action reference is always @main.

    That opens a stale window in both directions: a PR adding a mode to the driver
    would call a main without it and die at argument parsing, and a PR changing the
    driver would exercise main's copy rather than its own. Invoking the driver
    checked out at job.workflow_sha closes both, and is the pattern the sibling
    SDK scripts in this job already use.
    """
    steps = jobs["prepare-tenant"]["steps"]
    checkout_at = _index_of(steps, "application-sdk-scripts")
    verify_at = _index_of(steps, "e2e_tenant_lease.py")
    assert checkout_at is not None and verify_at is not None
    assert checkout_at < verify_at, "the driver must be fetched before it is run"

    fetch = steps[checkout_at]
    assert fetch["with"]["ref"] == "${{ job.workflow_sha }}"
    assert ".github/actions/e2e-tenant-lease" in fetch["with"]["sparse-checkout"], (
        "the sparse checkout must include the lease action, or the verify step "
        "cannot run this ref's driver"
    )
    assert (
        "application-sdk-scripts/.github/actions/e2e-tenant-lease"
        in (steps[verify_at]["run"])
    )


def _index_of(steps: list, needle: str) -> int | None:  # type: ignore[type-arg]
    for index, step in enumerate(steps):
        haystack = (
            f"{step.get('uses', '')} {step.get('run', '')} {step.get('with', {})}"
        )
        if needle in haystack:
            return index
    return None


def test_prepare_tenant_overrides_the_implicit_success_over_needs(jobs: dict) -> None:  # type: ignore[type-arg]
    """Without a status-check function the widened gate below is INERT.

    GitHub applies an implicit success() over every `needs` entry and skips the
    job before the `if:` is consulted, so on a failed lease leg prepare-tenant
    would still be skipped for every cloud and the per-cloud verify step would
    never run — the exact misbehaviour the widening exists to fix, passing its own
    expression test because a pure evaluator cannot model a needs-level skip.

    This is the same assertion `test_e2e_tolerates_skipped_but_not_failed_prepare`
    already makes for the legs. It is here because the gap it catches shipped once.
    """
    condition = " ".join(jobs["prepare-tenant"]["if"].split())
    assert "always()" in condition or "!cancelled()" in condition, (
        "prepare-tenant's `if:` has no status-check function, so GitHub's "
        "implicit success() over `needs` skips the job whenever ANY lease leg "
        "fails. The `!= 'skipped'` clause is dead without it."
    )


def test_prepare_tenant_names_every_need_it_requires(jobs: dict) -> None:  # type: ignore[type-arg]
    """always() lifts the implicit success() over ALL needs, so anything that must
    have succeeded has to be named — otherwise widening the lease gate silently
    also stopped requiring discovery and the image."""
    condition = " ".join(jobs["prepare-tenant"]["if"].split())
    for job in ("discover-e2e", "merge-e2e-image"):
        assert f"needs.{job}.result == 'success'" in condition, (
            f"missing `needs.{job}.result == 'success'`. With always() present, "
            "this is the only thing still requiring it."
        )


@pytest.mark.parametrize(
    ("lease_result", "should_install"),
    [
        ("success", True),
        # The case that was broken: another cloud's lease failed. This leg must
        # still get the chance to install, and its verify step decides.
        ("failure", True),
        ("cancelled", True),
        ("skipped", False),
    ],
)
def test_install_runs_whenever_the_lease_job_ran(
    lease_result: str, should_install: bool
) -> None:
    """Note the limit of this test, which is why the two above it exist: the
    evaluator models the `if:` expression only, not GitHub's needs-level skip. It
    passed on a gate that could never actually run."""
    expression = _load_gate("prepare-tenant")
    contexts = {
        "inputs": {"install-app-to-tenant": True},
        "needs": {
            "discover-e2e": {"result": "success"},
            "merge-e2e-image": {"result": "success"},
            "lease-tenant": {"result": lease_result},
        },
    }
    assert evaluate(expression, contexts) is should_install


@pytest.mark.parametrize("blocker", ["discover-e2e", "merge-e2e-image"])
def test_install_does_not_run_without_its_upstreams(blocker: str) -> None:
    # always() would otherwise let the install proceed with no image to install.
    expression = _load_gate("prepare-tenant")
    contexts = {
        "inputs": {"install-app-to-tenant": True},
        "needs": {
            "discover-e2e": {"result": "success"},
            "merge-e2e-image": {"result": "success"},
            "lease-tenant": {"result": "success"},
        },
    }
    contexts["needs"][blocker] = {"result": "failure"}
    assert evaluate(expression, contexts) is False


def test_the_legs_refuse_to_run_on_a_failed_lease(jobs: dict) -> None:  # type: ignore[type-arg]
    """A failed lease leaves prepare-tenant SKIPPED, and 'skipped' is benign in
    the legs' gate — so without lease-tenant named here the legs would run
    against a tenant nobody installed onto, with expected-app-version empty so
    the version check self-skips. A silently passing wrong-version run, from the
    machinery added to prevent them."""
    assert "lease-tenant" in jobs["e2e"]["needs"]
    gate = jobs["e2e"]["if"]
    assert "needs.lease-tenant.result == 'success'" in gate
    assert "needs.lease-tenant.result == 'skipped'" in gate


def test_the_lease_is_released_even_when_the_legs_fail(jobs: dict) -> None:  # type: ignore[type-arg]
    # A failed leg must still hand the tenant back, so the release is gated on
    # having taken the lease — not on the outcome of anything after it.
    gate = jobs["release-tenant"]["if"]
    assert "always()" in gate
    assert "e2e" in jobs["release-tenant"]["needs"]


@pytest.mark.parametrize(
    ("lease_result", "should_release"),
    [
        ("success", True),
        # THE case that was broken. lease-tenant is a per-cloud MATRIX job, so
        # `.result` is the aggregate: one cloud's acquire timing out made it
        # 'failure' and skipped the release for EVERY cloud, including the legs
        # that did acquire. Their leases then waited for the next contender's
        # reaper instead of being handed back — directly against this job's stated
        # purpose. Gating on "ran" rather than "succeeded" fixes it.
        ("failure", True),
        ("cancelled", True),
        # Never ran, so there is nothing to release.
        ("skipped", False),
    ],
)
def test_release_runs_whenever_any_lease_leg_may_hold_a_tenant(
    lease_result: str, should_release: bool
) -> None:
    """Evaluated rather than pattern-matched: `&&` binds tighter than `||` in
    GitHub expressions, so a gate that merely *mentions* the right terms can
    still be wrong. Widening this is only safe because the release driver checks
    ownership before deleting, so a leg that never acquired no-ops."""
    expression = _load_gate("release-tenant")
    assert (
        evaluate(expression, {"needs": {"lease-tenant": {"result": lease_result}}})
        is should_release
    )


def _load_gate(job: str) -> str:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"][job]["if"]


def test_the_lease_wait_fits_inside_the_job_timeout(jobs: dict) -> None:  # type: ignore[type-arg]
    """If the runner's timeout fires first, a bare "job cancelled after Nm"
    replaces the script's error — which is the one place the holding run is
    named, and therefore the only actionable output this job produces."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/e2e-tenant-lease/action.yaml").read_text(
            encoding="utf-8"
        )
    )
    wait_seconds = int(action["inputs"]["wait-seconds"]["default"])
    timeout_seconds = int(jobs["lease-tenant"]["timeout-minutes"]) * 60
    assert timeout_seconds > wait_seconds


#: The jobs a lease is actually HELD across: acquired before the install, released
#: after the last leg. The TTL is measured from the acquisition time the holder
#: records for itself, so only this span counts — none of the pre-lease work
#: (discovery, image build, manifest merge, runner queue time) does.
_LEASE_HELD_ACROSS = ("prepare-tenant", "e2e")


def test_the_lease_ttl_cannot_fire_on_a_healthy_holder(jobs: dict) -> None:  # type: ignore[type-arg]
    """The TTL breaking a LIVE holder's lease puts a second installer on the
    tenant — the exact race the lease exists to close — so it has to clear the
    longest legitimate hold with room to spare.

    Derived from the workflow's own timeouts rather than hard-coded, so raising
    either of them forces the TTL up instead of quietly eating the margin.
    """
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/e2e-tenant-lease/action.yaml").read_text(
            encoding="utf-8"
        )
    )
    ttl_seconds = int(action["inputs"]["ttl-seconds"]["default"])
    hold_seconds = (
        sum(int(jobs[job]["timeout-minutes"]) for job in _LEASE_HELD_ACROSS) * 60
    )

    assert ttl_seconds > hold_seconds, (
        f"ttl-seconds ({ttl_seconds}s) does not clear the longest legitimate hold "
        f"({'+'.join(_LEASE_HELD_ACROSS)} = {hold_seconds}s). A healthy holder "
        "would have its lease broken mid-run and a second installer would start."
    )
    # Runner queue time between the held jobs is unbounded, so clearing the sum
    # exactly is not enough; require real headroom rather than a one-second pass.
    assert ttl_seconds >= 1.5 * hold_seconds, (
        f"ttl-seconds ({ttl_seconds}s) clears the hold ({hold_seconds}s) but "
        "leaves little room for runner queue time, which has no timeout"
    )


def test_the_lease_wait_budget_is_the_operator_facing_signal(jobs: dict) -> None:  # type: ignore[type-arg]
    """The TTL is deliberately generous, so it must not be what tells a human the
    tenant is stuck — the wait budget has to fail long before it, or a blocked run
    sits silently for hours instead of reporting who holds the tenant."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/e2e-tenant-lease/action.yaml").read_text(
            encoding="utf-8"
        )
    )
    wait_seconds = int(action["inputs"]["wait-seconds"]["default"])
    ttl_seconds = int(action["inputs"]["ttl-seconds"]["default"])
    assert wait_seconds < ttl_seconds


def test_the_lease_and_install_fan_out_over_the_same_clouds(jobs: dict) -> None:  # type: ignore[type-arg]
    # A cloud the lease missed and the install then ran against is an
    # unserialised install, not a visible failure.
    assert (
        jobs["release-tenant"]["strategy"]["matrix"]
        == (jobs["prepare-tenant"]["strategy"]["matrix"])
    )


def test_the_lease_set_and_the_install_matrix_come_from_one_discovery(
    jobs: dict,  # type: ignore[type-arg]
) -> None:
    """The acquire takes a LIST and the install fans out over a MATRIX, so they
    can no longer be compared string-for-string. They are pinned to the same
    discovery STEP instead — `discover-clouds`, the clouds-only call — because
    that removes the possibility of disagreement rather than testing for it.
    """
    outputs = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        "discover-e2e"
    ]["outputs"]
    assert outputs["cloud-list"] == "${{ steps.discover-clouds.outputs.clouds }}"
    assert outputs["cloud-matrix"] == "${{ steps.discover-clouds.outputs.matrix }}"

    assert (
        _lease_step(jobs, "lease-tenant")["with"]["clouds"]
        == "${{ needs.discover-e2e.outputs.cloud-list }}"
    )
    assert "cloud-matrix" in jobs["prepare-tenant"]["strategy"]["matrix"]


def test_a_failed_lease_fails_the_install_rather_than_skipping_it(jobs: dict) -> None:  # type: ignore[type-arg]
    """Load-bearing in the opposite direction to how it reads.

    Acquisition is all-or-nothing, so a failed lease means the run holds nothing
    — and tightening prepare-tenant's gate to `== 'success'` would SKIP the
    install. The e2e legs deliberately tolerate a skipped prepare-tenant, so they
    would then run against whatever version the tenant happens to serve (FND-31).
    Letting the install run and fail on its own lease check is what stops them.
    """
    assert (
        evaluate(
            _load_gate("prepare-tenant"),
            {
                "inputs": {"install-app-to-tenant": True},
                "needs": {
                    "discover-e2e": {"result": "success"},
                    "merge-e2e-image": {"result": "success"},
                    "lease-tenant": {"result": "failure"},
                },
            },
        )
        is True
    )


def test_the_gate_is_told_about_the_lease() -> None:
    """Otherwise a lease failure reaches the gate only as the downstream "matrix
    skipped" anomaly, which reads as a workflow misconfiguration rather than
    "the tenant was busy"."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    # Both gate call sites: the required check and the cross-repo callback.
    assert text.count("lease-tenant-result:") == 2


# ── The two cloud fan-outs must agree ────────────────────────────────────────


def test_suite_and_cloud_discovery_use_the_same_clouds_expression(jobs: dict) -> None:  # type: ignore[type-arg]
    """A prepare-tenant that missed a cloud the legs then ran against is a
    silent coverage hole, not a visible failure — so the two `clouds:` inputs
    are asserted identical rather than merely both present.

    Keyed on the discovery action's `with.clouds` rather than on every line
    reading `clouds:`, which also matched the job OUTPUT of the same name added
    for the scorecard's cross-CSP record (FND-34) — an unrelated key that has no
    fan-out to agree with.
    """
    exprs = [
        step["with"]["clouds"]
        for step in jobs["discover-e2e"]["steps"]
        if "discover-e2e-suites@" in str(step.get("uses", ""))
    ]
    assert len(exprs) == 2, (
        f"expected exactly two discovery invocations, found {len(exprs)} — the "
        "suite fan-out and the cloud-only fan-out prepare-tenant installs from"
    )
    assert exprs[0] == exprs[1], (
        "the suite fan-out and the cloud fan-out resolve different cloud lists; "
        f"{exprs[0]!r} vs {exprs[1]!r}"
    )


def test_the_cloud_fallback_is_guarded_on_the_count_not_the_string(jobs: dict) -> None:  # type: ignore[type-arg]
    """An empty cloud list must yield ONE leg, never zero.

    A repo without the tenant-matrix secret resolves to `--clouds none`, which
    emits `{"include":[]}` — a perfectly non-empty string. So the natural
    `matrix || fallback` never takes the fallback, the job expands to zero legs,
    and a matrix job with zero legs is not a skipped job: it does not exist.
    `needs.prepare-tenant.result` is then never success-or-skipped and every job
    gated on it vanishes, which deleted an entire e2e matrix on the first live
    install run.

    The count is the only thing that carries the distinction, which is why
    e2e-full-reusable.yaml has always guarded on it.
    """
    matrix = " ".join(jobs["prepare-tenant"]["strategy"]["matrix"].split())
    assert "cloud-count != '0'" in matrix, (
        "prepare-tenant's matrix falls back on the matrix STRING being empty, "
        'which never happens — `{"include":[]}` is truthy. Guard on '
        "`needs.discover-e2e.outputs.cloud-count != '0'` instead, or this job "
        "expands to zero legs and takes the e2e legs with it."
    )
    assert '{"include":[{"cloud":""}]}' in matrix, (
        "the fallback must be a single leg with a defined-but-empty `cloud` — "
        "that is what makes the tenant resolver take its single-tenant path"
    )


def test_both_reusables_fall_back_the_same_way() -> None:
    """The two cloud fan-outs must not drift in *mechanism*, only in wiring.

    tests-reusable.yaml's version was written by copying the intent of
    e2e-full-reusable.yaml's without its mechanism — the count guard — and that
    is precisely the bug above. Pinning them together is cheaper than
    rediscovering it on the next live run.

    Parsed, not grepped: `count != '0'` also guards three unrelated job gates
    in tests-reusable.yaml, so a whole-file text search stays green even if the
    fan-out matrix itself loses the guard. Asserting on the parsed
    `strategy.matrix` of each cloud fan-out job binds the drift guard to the
    expression that actually expands to zero legs.
    """
    full = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/e2e-full-reusable.yaml").read_text(
            encoding="utf-8"
        )
    )
    tests = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    fallback = '{"include":[{"cloud":""}]}'
    fan_outs = (
        ("e2e-full-reusable", full["jobs"]["e2e-full"]),
        ("tests-reusable", tests["jobs"]["prepare-tenant"]),
    )
    for name, job in fan_outs:
        matrix = " ".join(job["strategy"]["matrix"].split())
        assert (
            fallback in matrix
        ), f"{name}'s cloud fan-out no longer carries the single-leg fallback"
        assert "count != '0'" in matrix, (
            f"{name}'s cloud matrix no longer guards on the count. Falling back "
            "on the matrix string alone silently produces a zero-leg job."
        )


def test_cloud_matrix_output_is_exposed(jobs: dict) -> None:  # type: ignore[type-arg]
    outputs = jobs["discover-e2e"]["outputs"]
    # Both, and both from the same step: the matrix says which clouds, the count
    # says whether there is a cloud dimension at all, and the consumer needs the
    # second to interpret the first.
    for key in ("cloud-matrix", "cloud-count"):
        assert key in outputs, f"discover-e2e no longer exposes {key}"
        assert "discover-clouds" in outputs[key]


# ── No expression interpolation into run: in the jobs this change adds ───────
# Scoped to the two new jobs, not the whole file: tests-reusable.yaml already
# carries ~6 pre-existing instances of the pattern in jobs this change does not
# own, and ~49 exist across the repo. Guarding only what is added keeps this
# assertion true from birth instead of born-disabled.


@pytest.mark.parametrize("job", ["build-e2e-image", "prepare-tenant"])
def test_new_jobs_route_values_through_env_not_into_run(jobs: dict, job: str) -> None:  # type: ignore[type-arg]
    """`github.head_ref` is attacker-controlled on a fork PR; `deploy_config` is
    multi-line YAML that would break the command outright. Both must arrive as
    env vars and be referenced as quoted shell variables."""
    offenders: list[str] = []
    for step in jobs[job].get("steps") or []:
        script = step.get("run")
        if not isinstance(script, str):
            continue
        for match in re.findall(r"\$\{\{[^}]*\}\}", script):
            offenders.append(f"{step.get('name', '?')}: {match.strip()}")
    assert not offenders, (
        f"{job} interpolates expressions directly into run:, which splices "
        "attacker-influenced or multi-line values into the shell. Pass them via "
        f'env: and reference quoted "$VARS". Offenders: {offenders}'
    )


# ── A tenant we cannot scope a release to is rejected up front ───────────────


def test_a_missing_tenant_id_is_rejected_before_anything_is_published(
    jobs: dict,
) -> None:  # type: ignore[type-arg]
    """The gate must sit between tenant resolution and the atlan.yaml parse.

    The resolver exports ``E2E_TENANT_ID`` only from a matrix entry carrying
    ``tenant_id``; the legacy single-tenant fallback has no entry to carry one.
    Without this step the job got as far as the publish and then failed inside the
    script on an empty ``--tenant``, several steps past the actual cause.

    Failing rather than skipping is the deliberate half: a skipped install leaves
    prepare-tenant green, the tenant on whatever version it was already running,
    and every leg reding on its own version check — one confusing failure per leg
    in place of one clear failure here.
    """
    names = [str(step.get("name", "")) for step in jobs["prepare-tenant"]["steps"]]
    gate = [
        step
        for step in jobs["prepare-tenant"]["steps"]
        if str(step.get("name", "")) == "Require a tenant ID before publishing anything"
    ]
    assert len(gate) == 1, "the tenant-ID gate is gone; the install would fail late"
    assert gate[0]["if"] == "env.E2E_TENANT_ID == ''", (
        "the gate must fire on an unresolved tenant ID specifically, and via the "
        "env context so the branch stays out of the run: block (ci.md)"
    )
    assert "exit 1" in gate[0]["run"], (
        "the gate must fail the job. A warning would leave the tenant unprepared "
        "and push the failure into every leg's version check instead."
    )

    position = names.index("Require a tenant ID before publishing anything")
    for later in ("Read atlan.yaml", "Install the app under test"):
        assert position < names.index(later), (
            f"the tenant-ID gate must run before {later!r} — the whole point is "
            "rejecting the misconfiguration before any work or any publish"
        )


# ── Two checks, at two altitudes, neither redundant (FND-203) ────────────────
# The install path has one precondition ("a tenant_id can be resolved") that is
# knowable in two halves at two different times, so it is checked twice. Nothing
# in the workflow says that, and each check looks redundant next to the other —
# which is exactly how one of them gets deleted.

#: In discover-e2e, the first job on the e2e path. Catches "no tenant matrix
#: shared with this repo at all", the openapi case: knowable from a secret's
#: presence, so it costs seconds.
_EARLY_CHECK = "Require the tenant matrix on the install path"

#: In prepare-tenant, after tenant resolution. Catches "matrix present, this
#: cloud's entry has no `tenant_id`" — invisible to the early check, which only
#: ever sees whether the secret exists.
_LATE_CHECK = "Require a tenant ID before publishing anything"

#: The distinct-id breadcrumb, the one step allowed to precede the early check.
#: It is a free ``echo`` — the workflow header comment names it as the only thing
#: ahead of the gate, so a costly ``run:`` inserted before the check must trip the
#: placement test rather than slip past a narrower first-``uses:`` comparison.
_DISTINCT_ID_BREADCRUMB = "distinct-id ${{ inputs.distinct-id }}"


def _named_step(jobs: dict, job: str, name: str) -> dict:  # type: ignore[type-arg]
    matches = [s for s in jobs[job]["steps"] if str(s.get("name", "")) == name]
    assert (
        len(matches) == 1
    ), f"expected exactly one {name!r} step in {job}, found {len(matches)}"
    return matches[0]


def test_both_install_preconditions_are_checked(jobs: dict) -> None:  # type: ignore[type-arg]
    """Neither check subsumes the other, so removing either is a regression.

    ``HAS_TENANT_MATRIX`` proves the secret *exists*; it says nothing about
    whether the cloud's entry inside it carries a ``tenant_id``. So the early
    check cannot cover the late one's case. And the late check cannot cover the
    early one's cheaply: it runs after two per-arch image builds and a manifest
    merge, which is the ~4 minutes of runner time FND-203 is about.

    Dropping the early one restores the waste. Dropping the late one lets a run
    with a matrix but no ``tenant_id`` for its cloud reach the publish and fail
    inside the driver on an empty ``--tenant``, several steps past the cause.
    """
    _named_step(jobs, "discover-e2e", _EARLY_CHECK)
    _named_step(jobs, "prepare-tenant", _LATE_CHECK)


def test_the_install_path_precondition_is_checked_at_discovery(jobs: dict) -> None:  # type: ignore[type-arg]
    step = _named_step(jobs, "discover-e2e", _EARLY_CHECK)
    condition = " ".join(str(step["if"]).split())

    assert "inputs.install-app-to-tenant" in condition, (
        "the early check must fire only on the install path — a caller that never "
        "installs is entitled to the single-tenant fallback, which is what the "
        "'Warn that the cross-CSP matrix is unavailable' step covers instead"
    )
    assert "env.HAS_TENANT_MATRIX == ''" in condition, (
        "the early check must key on the tenant-matrix signal; a step `if:` cannot "
        "read the `secrets` context directly, which is why the job carries it as env"
    )
    assert "exit 1" in step["run"], (
        "the early check must FAIL the run. A warning would let it go on to spend "
        "two image builds discovering the same thing in prepare-tenant."
    )


@pytest.mark.parametrize("install", [True, False])
@pytest.mark.parametrize("signal", ["", "true"])
def test_the_early_check_fires_only_on_the_install_path_without_a_matrix(
    jobs: dict,  # type: ignore[type-arg]
    install: bool,
    signal: str,
) -> None:
    """The full condition, evaluated — substring asserts cannot see a broken gate.

    Matching on the two operand substrings stays green under an edit that keeps
    both while wrecking the logic (an appended ``|| true``, regrouped operators).
    Evaluating the whole expression across the install × signal truth table pins
    what the gate actually does: fire only when installing with no matrix.
    """
    step = _named_step(jobs, "discover-e2e", _EARLY_CHECK)
    fires = evaluate(
        step["if"],
        {
            "inputs": {"install-app-to-tenant": install},
            "env": {"HAS_TENANT_MATRIX": signal},
        },
    )
    assert fires is (install and signal == "")


def test_the_early_check_precedes_every_step_that_costs_anything(jobs: dict) -> None:  # type: ignore[type-arg]
    """Placement is the entire value: a correct check in the wrong place saves
    nothing. It must precede the checkout and both discovery invocations, so the
    job fails in seconds rather than after the tree is fetched and globbed.

    Measuring only against the first ``uses:`` step would miss a costly ``run:``
    step inserted ahead of the check, so everything before it must be the named
    distinct-id breadcrumb — the one free ``echo`` the workflow header allows."""
    steps = jobs["discover-e2e"]["steps"]
    labels = [str(step.get("name") or step.get("uses", "")) for step in steps]
    position = labels.index(_EARLY_CHECK)

    first_uses = next(
        index for index, step in enumerate(steps) if step.get("uses") is not None
    )
    assert position < first_uses, (
        f"the early check runs after {labels[first_uses]!r}; it needs no checkout "
        "and no action, so nothing may precede it except the distinct-id breadcrumb"
    )

    preceding = steps[:position]
    assert [str(step.get("name", "")) for step in preceding] == [
        _DISTINCT_ID_BREADCRUMB
    ], (
        "a step that is not the distinct-id breadcrumb runs before the early "
        "check; anything with a `uses:` or a costly `run:` ahead of it delays the "
        "failure past the point where it saves the two image builds"
    )

    for later in ("Discover suites", "Discover clouds"):
        assert position < labels.index(later)


@pytest.mark.parametrize(
    "secret,expected",
    [
        ("", ""),
        ('{"aws":{"tenant":"t","tenant_id":"i"}}', "true"),
    ],
)
def test_the_early_checks_signal_is_empty_exactly_when_the_secret_is(
    jobs: dict,  # type: ignore[type-arg]
    secret: str,
    expected: str,
) -> None:
    """The signal must resolve to '' — not 'false' — when the secret is absent.

    The check reads ``env.HAS_TENANT_MATRIX == ''``, so a plausible-looking
    simplification of the expression to a bare ``secrets.X != ''`` would render
    the signal ``'false'``, never match, and disable the check silently. That is
    the failure mode worth a test: the loud direction (losing the job-level env
    entirely, making the comparison always true) fails every install-path run on
    its first use.
    """
    env = jobs["discover-e2e"].get("env") or {}
    assert "HAS_TENANT_MATRIX" in env, (
        "discover-e2e no longer carries HAS_TENANT_MATRIX at JOB level, so the "
        "early check's `if:` cannot see it — a step condition cannot read `secrets`"
    )
    resolved = evaluate_operand(
        env["HAS_TENANT_MATRIX"], {"secrets": {"E2E_TENANT_MATRIX_JSON": secret}}
    )
    assert resolved == expected


@pytest.mark.parametrize(
    "discovery,builds",
    [("failure", False), ("success", True)],
)
def test_a_failed_discovery_skips_the_image_builds(
    jobs: dict,  # type: ignore[type-arg]
    discovery: str,
    builds: bool,
) -> None:
    """What converts the early failure into an actual saving.

    Failing discover-e2e only avoids the two builds because build-e2e-image gates
    on discover-e2e having SUCCEEDED. Widen that to `always()` or to
    `result != 'skipped'` — both of which read as harmless robustness — and the
    builds run anyway, at which point the early check saves nothing at all.
    """
    ran = evaluate(
        jobs["build-e2e-image"]["if"],
        {
            "inputs": {"install-app-to-tenant": True},
            "needs": {"discover-e2e": {"result": discovery, "outputs": {"count": "2"}}},
        },
    )
    assert ran is builds


def test_job_timeout_stays_above_the_scripts_own_waits(jobs: dict) -> None:  # type: ignore[type-arg]
    """The runner's timeout must not be able to fire before the script's.

    The publish retries while the marketplace service is not answering, then the
    install retries while LM's catalog snapshot catches up, the deployment is
    polled, and a FAILED verdict that names only benign pod churn then settles —
    all four bounded by the script's defaults, since the step overrides none of
    them. They are sequential in the worst case, so the budget is their sum. If
    the job budget is under it, a slow sync reports as a bare
    "job cancelled after Nm" and the actionable error the script was about to
    print is never written: the diagnosis-hostile failure this whole job exists to
    avoid. Derived from the script's constants, so raising one of those fails here
    rather than silently making a job timeout reachable.
    """
    waits = (
        app.DEFAULT_PUBLISH_RETRY_SECONDS
        + app.DEFAULT_INSTALL_RETRY_SECONDS
        + app.DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS
        + app.DEFAULT_SETTLE_SECONDS
    ) // 60
    required = round(waits / _MAX_WAIT_SHARE)
    actual = jobs["prepare-tenant"]["timeout-minutes"]
    assert actual >= required, (
        f"prepare-tenant allows {actual} min but the script can spend {waits} min "
        "of it waiting, so a slow runner makes the GHA timeout reachable before "
        f"the script's own. Raise it to at least {required}."
    )

    install = [
        s
        for s in jobs["prepare-tenant"]["steps"]
        if str(s.get("name", "")) == "Install the app under test"
    ][0]
    for flag in (
        "--publish-retry-seconds",
        "--install-retry-seconds",
        "--timeout-seconds",
        "--settle-seconds",
    ):
        assert flag not in install["run"], (
            f"the step now passes {flag}, so the budget above is no longer the "
            "script's default — compute the timeout from the passed value instead"
        )


def test_install_step_does_not_thread_app_id(jobs: dict) -> None:  # type: ignore[type-arg]
    """app_id has one source: atlan.yaml, read (and validated) by the script.

    Threading it through a step output as well would give the value two sources
    that can disagree, for no gain — the job already runs from the repo root.
    """
    install = [
        s
        for s in jobs["prepare-tenant"]["steps"]
        if str(s.get("name", "")) == "Install the app under test"
    ]
    assert len(install) == 1
    assert "--app-id" not in install[0]["run"]


# ── The action-side verify self-skips ────────────────────────────────────────


def test_verify_step_self_skips_when_no_version_expected() -> None:
    """Empty `expected-app-version` must skip the check entirely.

    17 repos call this action directly with no install step. They are
    deliberately left alone for now, so the verify must be inert for them
    rather than reding their e2e.
    """
    action = yaml.safe_load(_SDR_ACTION.read_text(encoding="utf-8"))
    assert action["inputs"]["expected-app-version"]["default"] == ""

    steps = action["runs"]["steps"]
    verify = [s for s in steps if "Verify the tenant runs" in str(s.get("name", ""))]
    assert len(verify) == 1, "expected exactly one tenant-version verify step"
    assert verify[0]["if"] == "inputs.expected-app-version != ''"

    # It has to sit before pytest: the window that matters is the one immediately
    # before the DAG is submitted, since Heracles fetches the manifest from the
    # deployed pod at submit time.
    names = [str(s.get("name", "")) for s in steps]
    assert names.index(str(verify[0]["name"])) < names.index(
        "Run SDR integration tests"
    ), "the version check must run before pytest, not after"

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

import e2e_tenant_app as app  # noqa: E402

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


# ── Opt-in, so unadopted repos are untouched ─────────────────────────────────


def test_install_is_off_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    # Turning this on makes tenant health a gate. 25+ repos consume this
    # workflow; it must not switch on under them.
    #
    # `workflow[True]`: YAML 1.1 resolves the bare key `on` to the boolean true,
    # so safe_load never yields the string "on". Both spellings are accepted here
    # so the guard survives a future quoted `"on":`.
    triggers = workflow.get("on", workflow.get(True))
    spec = triggers["workflow_call"]["inputs"]["install-app-to-tenant"]
    assert spec["default"] is False
    assert spec["type"] == "boolean"


@pytest.mark.parametrize("job", ["build-e2e-image", "prepare-tenant"])
def test_new_jobs_are_gated_on_the_input(jobs: dict, job: str) -> None:  # type: ignore[type-arg]
    assert "inputs.install-app-to-tenant" in jobs[job]["if"], (
        f"{job} must not run when install-app-to-tenant is off — otherwise every "
        "existing caller pays for a build and an install it did not ask for"
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


def test_prepare_tenant_serialises_per_tenant_not_per_ref(jobs: dict) -> None:  # type: ignore[type-arg]
    concurrency = jobs["prepare-tenant"]["concurrency"]
    group = concurrency["group"]

    # The installation is tenant-scoped, so the group has to identify the TENANT
    # (app + cloud). Keying on github.ref would let two PRs install different
    # versions to the same tenant concurrently, and the loser is silently what
    # the tenant ends up running.
    assert "inputs.app-name" in group and "matrix.cloud" in group
    assert "github.ref" not in group, (
        "keying on the ref permits concurrent installs to the same tenant from "
        "different refs, which is the race this group exists to narrow"
    )
    # Cancelling mid-install abandons the deployment and leaves the tenant in
    # whatever state LM reached.
    assert concurrency["cancel-in-progress"] is False


# ── The two cloud fan-outs must agree ────────────────────────────────────────


def test_suite_and_cloud_discovery_use_the_same_clouds_expression() -> None:
    """A prepare-tenant that missed a cloud the legs then ran against is a
    silent coverage hole, not a visible failure — so the two `clouds:` inputs
    are asserted identical rather than merely both present."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    exprs = [
        line.split("clouds:", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("clouds:")
    ]
    assert len(exprs) == 2, f"expected exactly two `clouds:` inputs, found {len(exprs)}"
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
    """
    full = (_REPO_ROOT / ".github/workflows/e2e-full-reusable.yaml").read_text(
        encoding="utf-8"
    )
    tests = _WORKFLOW.read_text(encoding="utf-8")
    fallback = '{"include":[{"cloud":""}]}'
    for name, text in (("e2e-full-reusable", full), ("tests-reusable", tests)):
        assert fallback in text, f"{name} no longer carries the single-leg fallback"
        assert "count != '0'" in text, (
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


def test_job_timeout_stays_above_the_scripts_own_waits(jobs: dict) -> None:  # type: ignore[type-arg]
    """The runner's timeout must not be able to fire before the script's.

    The install retries while LM's catalog snapshot catches up, then polls the
    deployment — both bounded by the script's defaults, since the step overrides
    neither. If the job budget is under their sum, a slow sync reports as a bare
    "job cancelled after Nm" and the actionable error the script was about to
    print is never written: the diagnosis-hostile failure this whole job exists to
    avoid. Derived from the script's constants, so raising one of those fails here
    rather than silently making a job timeout reachable.
    """
    waits = (
        app.DEFAULT_INSTALL_RETRY_SECONDS + app.DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS
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
    for flag in ("--install-retry-seconds", "--timeout-seconds"):
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

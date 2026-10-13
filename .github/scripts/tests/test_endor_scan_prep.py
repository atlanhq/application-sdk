"""Tests for endor_scan_prep.py, the Endor scan's credential gate and tarball prep.

Both decisions used to live inline in the Endor jobs' ``run:`` blocks
(build-and-scan.yaml and daily-security-scan.yml), where docs/standards/ci.md
forbids branching. The behavioural tests pin every branch; the cross-file guards
at the bottom pin the workflow wiring the review asked for: Endor reading the
tarball its own job built (no image artifact), caller input travelling through
``env:`` rather than ``${{ }}`` interpolation into shell, and the
provenance-pinned script checkout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import endor_scan_prep as prep  # noqa: E402

GITHUB_DIR = _SCRIPTS_DIR.parent
BUILD_AND_SCAN = GITHUB_DIR / "workflows" / "build-and-scan.yaml"
HOURLY_SCAN = GITHUB_DIR / "workflows" / "daily-security-scan.yml"
ACTIONLINT = GITHUB_DIR / "actionlint.yaml"
SCRIPT_NAME = "endor_scan_prep.py"

# The jobs running an Endor scan and the checkout path each one invokes the
# script from. In build-and-scan.yaml the scan is a block of `Endor: ...` steps
# inside the build job (FND-3319), so the guards below look at those steps only.
ENDOR_JOBS = [
    (BUILD_AND_SCAN, "build", "_sdk"),
    (HOURLY_SCAN, "endor-base-scan", "."),
]
ENDOR_STEP_PREFIX = "Endor: "

# Shell keywords that mean a `run:` block branches (docs/standards/ci.md).
BRANCHING_SHELL = re.compile(
    r"^\s*(if|then|else|elif|fi|case|esac|for|while|until|done)\b", re.M
)


# ── credentials ─────────────────────────────────────────────────────────────────


def test_present_key_is_configured():
    assert prep.credentials_configured("endr_key") is True


@pytest.mark.parametrize("key", [None, "", "   ", "\n"])
def test_missing_or_blank_key_is_not_configured(key):
    assert prep.credentials_configured(key) is False


def test_run_credentials_reports_true_and_stays_quiet(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert prep.run_credentials({"ENDOR_KEY": "k"}) == 0
    assert out.read_text() == "configured=true\n"
    assert capsys.readouterr().out == ""


def test_run_credentials_reports_false_and_notices(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert prep.run_credentials({}) == 0
    assert out.read_text() == "configured=false\n"
    assert "::notice title=Endor scan skipped::" in capsys.readouterr().out


def test_credential_value_never_reaches_output_or_log(tmp_path, monkeypatch, capsys):
    secret = "s3cret-endor-key-value"
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    prep.run_credentials({"ENDOR_KEY": secret})
    captured = capsys.readouterr()
    assert secret not in out.read_text()
    assert secret not in captured.out + captured.err


# ── materialise: the two branches ───────────────────────────────────────────────


def test_prebuilt_pulls_and_saves_with_an_explicit_platform():
    commands, ref = prep.plan_materialise("ghcr.io/atlanhq/x:1.2.3", "", "", "/t.tar")
    assert ref == "ghcr.io/atlanhq/x:1.2.3"
    assert [c[:2] for c in commands] == [["docker", "pull"], ["docker", "save"]]
    for cmd in commands:
        # Docker 29 rejects `docker save` on a multi-arch ref without this.
        assert cmd[2:4] == ["--platform", "linux/amd64"]
        assert cmd[-1] == "ghcr.io/atlanhq/x:1.2.3"
    assert "-o" in commands[1] and "/t.tar" in commands[1]


def test_prebuilt_path_does_not_need_repo_or_ref():
    commands, ref = prep.plan_materialise("registry/base:3", "", "", "/t.tar")
    assert commands and ref == "registry/base:3"


def test_local_path_runs_no_docker_and_names_the_published_image():
    commands, ref = prep.plan_materialise(
        "", "atlan-example-app", "0123456789abcdef0123456789abcdef01234567", "/t.tar"
    )
    assert commands == []
    assert ref == "ghcr.io/atlanhq/atlan-example-app:0123456"


def test_a_short_ref_is_used_whole():
    assert prep.local_ref("repo", "abc") == "ghcr.io/atlanhq/repo:abc"


@pytest.mark.parametrize(("repo", "ref"), [("", "abcdef0"), ("repo", ""), ("  ", " ")])
def test_local_path_requires_repo_and_ref(repo, ref):
    with pytest.raises(ValueError):
        prep.plan_materialise("", repo, ref, "/t.tar")


def test_caller_supplied_ref_is_data_not_shell(tmp_path, monkeypatch):
    """The SEC finding: `inputs.ref` used to be interpolated into `run:`, where
    `$(...)` executes. Through env it is a string that gets truncated like any
    other, and nothing is spawned on the local path."""
    tarball = tmp_path / "image.tar"
    tarball.write_bytes(b"tar")
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    calls: list[list[str]] = []
    env = {"REPO": "repo", "REF": "$(touch pwned)`id`", "TARBALL": str(tarball)}
    assert prep.run_materialise(env, run=calls.append) == 0
    assert calls == []
    assert out.read_text() == "ref=ghcr.io/atlanhq/repo:$(touch\n"


def test_run_materialise_prebuilt_end_to_end(tmp_path, monkeypatch):
    tarball = tmp_path / "base.tar"
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    calls: list[list[str]] = []

    def fake_docker(cmd: list[str]) -> None:
        calls.append(cmd)
        if cmd[1] == "save":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"tar")

    env = {"PREBUILT": "registry/base:3", "TARBALL": str(tarball)}
    assert prep.run_materialise(env, run=fake_docker) == 0
    assert [c[1] for c in calls] == ["pull", "save"]
    assert tarball.is_file()
    assert out.read_text() == "ref=registry/base:3\n"


def test_missing_tarball_fails_loudly(tmp_path, monkeypatch, capsys):
    """A tarball the download step never delivered must fail here with the
    cause, not minutes later inside endorctl."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    env = {"REPO": "repo", "REF": "abcdef0", "TARBALL": str(tmp_path / "nope.tar")}
    assert prep.run_materialise(env, run=lambda _c: None) == 1
    err = capsys.readouterr().err
    assert "::error::" in err and "nope.tar" in err
    assert not (tmp_path / "out").exists()


def test_missing_repo_is_an_error_not_a_bad_ref(tmp_path, monkeypatch, capsys):
    tarball = tmp_path / "image.tar"
    tarball.write_bytes(b"tar")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    env = {"REF": "abcdef0", "TARBALL": str(tarball)}
    assert prep.run_materialise(env, run=lambda _c: None) == 1
    assert "::error::REPO is empty" in capsys.readouterr().err


def test_tarball_defaults_to_the_build_job_output_path():
    assert prep.DEFAULT_TARBALL == "/tmp/image.tar"


# ── main(): the env-var contract the workflows rely on ──────────────────────────


def test_main_dispatches_credentials(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("ENDOR_KEY", "k")
    assert prep.main(["credentials"]) == 0
    assert out.read_text() == "configured=true\n"


def test_main_dispatches_materialise(tmp_path, monkeypatch):
    tarball = tmp_path / "image.tar"
    tarball.write_bytes(b"tar")
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("PREBUILT", raising=False)
    monkeypatch.setenv("REPO", "repo")
    monkeypatch.setenv("REF", "abcdef0123")
    monkeypatch.setenv("TARBALL", str(tarball))
    assert prep.main(["materialise"]) == 0
    assert out.read_text() == "ref=ghcr.io/atlanhq/repo:abcdef0\n"


def test_main_requires_a_subcommand():
    with pytest.raises(SystemExit):
        prep.main([])


# ── Cross-file guards: the workflow wiring ──────────────────────────────────────


def _job(path: Path, job_id: str) -> dict:
    doc = yaml.safe_load(path.read_text())
    job = (doc.get("jobs") or {}).get(job_id)
    assert isinstance(job, dict), f"{path.name} has no job {job_id!r}"
    return job


def _steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _step(job: dict, step_id: str) -> dict:
    for step in _steps(job):
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id {step_id!r}")


def _endor_steps(path: Path, job_id: str) -> list[dict]:
    """The steps that make up the Endor scan: the whole hourly job, or the
    `Endor: ...` block of build-and-scan's build job."""
    steps = _steps(_job(path, job_id))
    if path != BUILD_AND_SCAN:
        return steps
    endor = [s for s in steps if str(s.get("name", "")).startswith(ENDOR_STEP_PREFIX)]
    assert endor, f"no `{ENDOR_STEP_PREFIX}` steps in {path.name} {job_id}"
    return endor


@pytest.mark.parametrize(("path", "job_id", "checkout_path"), ENDOR_JOBS)
def test_endor_jobs_delegate_both_decisions_to_the_script(path, job_id, checkout_path):
    job = _job(path, job_id)
    prefix = "" if checkout_path == "." else f"{checkout_path}/"
    script = f"python3 {prefix}.github/scripts/{SCRIPT_NAME}"
    assert _step(job, "creds")["run"].strip() == f"{script} credentials"
    assert _step(job, "image")["run"].strip() == f"{script} materialise"


@pytest.mark.parametrize(("path", "job_id", "_"), ENDOR_JOBS)
def test_endor_jobs_have_no_inlined_conditional_shell(path, job_id, _):
    offenders = [
        s.get("name") or s.get("id")
        for s in _endor_steps(path, job_id)
        if isinstance(s.get("run"), str) and BRANCHING_SHELL.search(s["run"])
    ]
    assert not offenders, f"{path.name} {job_id}: branching shell in {offenders}"


@pytest.mark.parametrize(("path", "job_id", "_"), ENDOR_JOBS)
def test_no_expression_is_interpolated_into_endor_run_blocks(path, job_id, _):
    """Inputs reach the shell through `env:`, never `${{ }}` in the body."""
    offenders = [
        s.get("name") or s.get("id")
        for s in _endor_steps(path, job_id)
        if isinstance(s.get("run"), str) and "${{" in s["run"]
    ]
    assert not offenders, f"{path.name} {job_id}: `${{{{` inside run: {offenders}"


def test_caller_ref_travels_through_env():
    env = _step(_job(BUILD_AND_SCAN, "build"), "image").get("env") or {}
    assert env.get("REF") == "${{ inputs.ref || github.sha }}"
    assert env.get("PREBUILT") == "${{ inputs.image }}"
    assert env.get("TARBALL") == "/tmp/image.tar"


def test_no_image_artifact_travels_between_jobs():
    """FND-3319: the image is built and scanned in one job, so no step of
    build-and-scan.yaml uploads or downloads a `docker-image*` artifact -- not
    the first attempt, not the `-retry` copy, not a download of either."""
    doc = yaml.safe_load(BUILD_AND_SCAN.read_text())
    offenders = [
        f"{job_id}: {s.get('name') or s.get('id')}"
        for job_id, job in doc["jobs"].items()
        for s in _steps(job)
        if "-artifact@" in str(s.get("uses", ""))
        and "docker-image"
        in str((s.get("with") or {}).get("name", ""))
        + str((s.get("with") or {}).get("pattern", ""))
    ]
    assert not offenders, offenders


def test_endor_scans_the_tarball_its_own_job_built():
    """Both scanners read byte-identical image content: Endor is handed the
    same /tmp/image.tar the build step wrote and Trivy loaded, in that order."""
    steps = _steps(_job(BUILD_AND_SCAN, "build"))
    build = _step(_job(BUILD_AND_SCAN, "build"), "build-image")
    assert build["with"]["outputs"] == "type=docker,dest=/tmp/image.tar"
    scan = next(s for s in steps if "endorlabs/github-action" in str(s.get("uses")))
    image = _step(_job(BUILD_AND_SCAN, "build"), "image")
    assert scan["with"]["image_tar"] == "/tmp/image.tar"
    # The scan is gated on `steps.image.outcome == 'success'`. Reordered ahead
    # of `image`, that condition is false (the step has not run) and Endor is
    # silently skipped, so the order is part of the contract.
    assert scan["if"] == "steps.image.outcome == 'success'"
    assert steps.index(build) < steps.index(image) < steps.index(scan)


def test_endor_cannot_fail_the_required_build_check():
    """The build job carries `scan / Build Image`, a required context. Endor is
    report-only, so every one of its steps is continue-on-error, they all run
    after the Trivy results are uploaded, and the scan has its own timeout so a
    hang cannot eat the build's and Trivy's budget."""
    job = _job(BUILD_AND_SCAN, "build")
    steps = _steps(job)
    endor = _endor_steps(BUILD_AND_SCAN, "build")
    assert len(endor) == 4
    assert all(s.get("continue-on-error") is True for s in endor), endor
    assert steps.index(endor[0]) > steps.index(_step(job, "upload-trivy"))
    assert all(steps.index(s) > steps.index(endor[0]) for s in endor[1:])
    scan = next(s for s in endor if "endorlabs/github-action" in str(s.get("uses")))
    assert isinstance(scan.get("timeout-minutes"), int)


# Minutes the job cap reserves for the short, unbudgeted steps (checkout,
# buildx, login, docker load, Trivy install and cache, uploads, Endor prep).
SHORT_STEP_HEADROOM_MINUTES = 10

# The steps that can run long before the Endor scan. Each must carry its own
# budget, or the job cap cannot be shown to leave Endor its full one.
LONG_STEPS_BEFORE_ENDOR = ("build-image", "build-image-retry", "trivy-scan")


def test_job_cap_reserves_endors_full_budget():
    """The job timeout is a hard cap from job start, and continue-on-error does
    not protect a step from it: a job that times out during Endor fails the
    required `Build Image` context. So the cap must cover the worst case of
    every budgeted step before the scan, run back to back, plus the scan's
    own budget and headroom for the short steps. A tighter cap would let a
    slow but successful build and Trivy run turn report-only Endor into a
    gate."""
    job = _job(BUILD_AND_SCAN, "build")
    steps = _steps(job)
    scan = next(s for s in steps if "endorlabs/github-action" in str(s.get("uses")))
    for step_id in LONG_STEPS_BEFORE_ENDOR:
        step = _step(job, step_id)
        assert isinstance(step.get("timeout-minutes"), int), step_id
        assert steps.index(step) < steps.index(scan), step_id
    before = sum(
        s["timeout-minutes"]
        for s in steps[: steps.index(scan)]
        if isinstance(s.get("timeout-minutes"), int)
    )
    assert (
        before + scan["timeout-minutes"] + SHORT_STEP_HEADROOM_MINUTES
        <= job["timeout-minutes"]
    )


def test_at_most_two_jobs_and_both_required_contexts_survive():
    """FND-3319 budget: at most two billed jobs per run. The two names are the
    required contexts fleet rulesets carry (`scan / Build Image`,
    `scan / Security Gate`), so neither may be renamed or folded away.

    The third job, `queue-diff` (FND-3321), is billed only on a merge-queue
    entry, where it exists to let the other two skip; on every other event it
    is skipped. When the two skip, and so may never skip, is pinned in
    test_queue_entry_rechecks.py.

    The fourth, `scope` (FND-3328), is a two-step ubuntu-slim decision that
    runs only on a PR or queue entry and lets the other two skip everywhere
    but the bump-version PR; pinned in test_release_candidate_gates.py."""
    jobs = yaml.safe_load(BUILD_AND_SCAN.read_text())["jobs"]
    assert {job_id: job["name"] for job_id, job in jobs.items()} == {
        "scope": "Scan scope",
        "queue-diff": "Queue tree diff",
        "build": "Build Image",
        "security-gate": "Security Gate",
    }
    assert "github.event_name == 'merge_group'" in jobs["queue-diff"]["if"]
    assert jobs["build"]["needs"] == ["scope", "queue-diff"]
    assert jobs["security-gate"]["needs"] == ["scope", "queue-diff", "build"]


def test_endor_scan_script_checkout_is_provenance_pinned():
    """The reusable runs in the caller's repo, so the script comes from the SDK
    at the workflow's own SHA, never a caller-controlled ref, and into its own
    path so it cannot leave the workspace sparse."""
    job = _job(BUILD_AND_SCAN, "build")
    steps = _steps(job)
    checkouts = [
        s
        for s in steps
        if "actions/checkout@" in str(s.get("uses", ""))
        and (s.get("with") or {}).get("repository") == "atlanhq/application-sdk"
    ]
    assert len(checkouts) == 1
    with_block = checkouts[0]["with"]
    assert with_block["ref"] == "${{ job.workflow_sha }}"
    assert SCRIPT_NAME in str(with_block["sparse-checkout"])
    assert with_block["path"] == "_sdk"
    assert with_block["persist-credentials"] is False
    assert steps.index(checkouts[0]) < steps.index(_step(job, "creds"))
    # Fetched only after the image is built, so the SDK checkout can never end
    # up inside the app's Docker build context.
    assert steps.index(checkouts[0]) > steps.index(_step(job, "build-image"))


def test_hourly_scan_checkout_is_sparse_and_credential_free():
    steps = _steps(_job(HOURLY_SCAN, "endor-base-scan"))
    checkouts = [s for s in steps if "actions/checkout@" in str(s.get("uses", ""))]
    assert len(checkouts) == 1
    with_block = checkouts[0]["with"]
    assert SCRIPT_NAME in str(with_block["sparse-checkout"])
    assert with_block["persist-credentials"] is False
    env = _step(_job(HOURLY_SCAN, "endor-base-scan"), "image")["env"]
    assert env["PREBUILT"].startswith("registry.atlan.com/public/app-runtime-base:")
    assert env["TARBALL"] == "/tmp/base-image.tar"


def test_actionlint_carries_the_workflow_sha_false_positive_for_build_and_scan():
    """actionlint <=1.7.12 does not model `job.workflow_sha`; the ignore is what
    keeps pre-commit green while preserving the provenance pin."""
    config = yaml.safe_load(ACTIONLINT.read_text())
    entry = config["paths"][".github/workflows/build-and-scan.yaml"]
    assert 'property "workflow_sha" is not defined' in entry["ignore"]

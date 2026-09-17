"""Tests for endor_scan_prep.py, the Endor scan's credential gate and tarball prep.

Both decisions used to live inline in the Endor jobs' ``run:`` blocks
(build-and-scan.yaml and daily-security-scan.yml), where docs/standards/ci.md
forbids branching. The behavioural tests pin every branch; the cross-file guards
at the bottom pin the workflow wiring the review asked for: the retry-artifact
glob, caller input travelling through ``env:`` rather than ``${{ }}``
interpolation into shell, and the provenance-pinned script checkout.
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

# The Endor jobs and the checkout path each one invokes the script from.
ENDOR_JOBS = [
    (BUILD_AND_SCAN, "endor-scan", "_sdk"),
    (HOURLY_SCAN, "endor-base-scan", "."),
]

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
        for s in _steps(_job(path, job_id))
        if isinstance(s.get("run"), str) and BRANCHING_SHELL.search(s["run"])
    ]
    assert not offenders, f"{path.name} {job_id}: branching shell in {offenders}"


@pytest.mark.parametrize(("path", "job_id", "_"), ENDOR_JOBS)
def test_no_expression_is_interpolated_into_endor_run_blocks(path, job_id, _):
    """Inputs reach the shell through `env:`, never `${{ }}` in the body."""
    offenders = [
        s.get("name") or s.get("id")
        for s in _steps(_job(path, job_id))
        if isinstance(s.get("run"), str) and "${{" in s["run"]
    ]
    assert not offenders, f"{path.name} {job_id}: `${{{{` inside run: {offenders}"


def test_caller_ref_travels_through_env():
    env = _step(_job(BUILD_AND_SCAN, "endor-scan"), "image").get("env") or {}
    assert env.get("REF") == "${{ inputs.ref || github.sha }}"
    assert env.get("PREBUILT") == "${{ inputs.image }}"
    assert env.get("TARBALL") == "/tmp/image.tar"


def test_endor_scan_consumes_the_retry_artifact_like_trivy_does():
    """The CI finding: the build job's first upload is continue-on-error and its
    retry lands as `docker-image-retry`. A bare `name:` misses it silently.

    Asserted over EVERY download attempt in both jobs, not just the first: the
    download is itself retried now, and a retry that reverted to a bare `name:`
    would reintroduce the miss in precisely the case the retry exists for.
    """
    downloads = {
        job_id: [
            s
            for s in _steps(_job(BUILD_AND_SCAN, job_id))
            if "actions/download-artifact" in str(s.get("uses", ""))
        ]
        for job_id in ("trivy-scan", "endor-scan")
    }
    assert downloads["endor-scan"], "endor-scan must download the image"
    assert downloads["trivy-scan"], "reference Trivy download disappeared"
    # Same shape in both, so the two consumers cannot drift apart.
    for job_id, steps in downloads.items():
        for step in steps:
            with_block = step.get("with") or {}
            assert with_block.get("pattern") == "docker-image*", job_id
            assert with_block.get("merge-multiple") is True, job_id
            assert "name" not in with_block, job_id


def test_endor_scan_script_checkout_is_provenance_pinned():
    """The reusable runs in the caller's repo, so the script comes from the SDK
    at the workflow's own SHA, never a caller-controlled ref, and into its own
    path so it cannot leave the workspace sparse."""
    steps = _steps(_job(BUILD_AND_SCAN, "endor-scan"))
    checkouts = [s for s in steps if "actions/checkout@" in str(s.get("uses", ""))]
    assert len(checkouts) == 1
    with_block = checkouts[0]["with"]
    assert with_block["repository"] == "atlanhq/application-sdk"
    assert with_block["ref"] == "${{ job.workflow_sha }}"
    assert SCRIPT_NAME in str(with_block["sparse-checkout"])
    assert with_block["path"] == "_sdk"
    assert with_block["persist-credentials"] is False
    assert steps.index(checkouts[0]) < steps.index(
        _step(_job(BUILD_AND_SCAN, "endor-scan"), "creds")
    )


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

"""Wiring of the ``git_token`` BuildKit secret in the image builds (FND-4744).

A Dockerfile whose ``uv sync`` clones a private atlanhq git dependency reads a
GitHub token from the ``git_token`` build secret. The image builds used to pass
the org-admin ORG_PAT_GITHUB there. Inside the build that token is readable by
every build backend the ``uv sync`` runs, including third-party setup scripts.

The builds now mint a read-only fleet App token for it, scoped to the private
atlanhq repos the app names (lens round 5): the Dockerfile is caller code, so
the token must reach no repo the build does not need. These tests pin that
shape:

* no ``git_token=`` secret or ``git-token:`` input under ``.github/`` reads
  ORG_PAT_GITHUB, and every one in a workflow reads the job's image mint;
* the mint is read-only and scoped to the app's private repos
  (``.github/actions/scoped-private-deps-token``, pinned by
  ``_scoped_mint.py``: never unscoped, nothing minted when the app names no
  private repo), with no opt-in gate (a Dockerfile without the mount ignores
  the secret, and several callers of these builds set no opt-in flag), and an
  empty token does not fail the job (``require: "false"``) except in e2e, where
  the same mint serves the opted-in host-side ``uv sync``;
* the minted token reaches nothing but the build secret (and, in e2e, the
  sdr-e2e action's ``git-auth-token``, pinned in test_private_git_deps_wiring.py);
* GHCR login and push stay on ORG_PAT_GITHUB, because fleet packages are
  org-level and the App only has repo-level Packages permission.

The runner-side token for ``uv sync`` (FND-4740) is pinned separately, in
test_private_git_deps_wiring.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scoped_mint import NOT_REQUIRED, assert_scoped_mint  # noqa: E402

Step = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"

MINT_ID = "image-git-token"
#: e2e mints once for the image build and the host-side ``uv sync`` (FND-4901).
SHARED_MINT = {("tests-reusable.yaml", "e2e"): "git-deps-token"}


def _mint_id(workflow_file: str, job: str) -> str:
    return SHARED_MINT.get((workflow_file, job), MINT_ID)


def _token_ref(workflow_file: str, job: str) -> str:
    return f"steps.{_mint_id(workflow_file, job)}.outputs.token"


#: (workflow file, job id, step locator) for every step that hands a token to
#: an image build. The locator is the step's id, else its name.
SITES = [
    ("tests-reusable.yaml", "build-e2e-image", "build"),
    (
        "tests-reusable.yaml",
        "e2e",
        "atlanhq/application-sdk/.github/actions/sdr-e2e@main",
    ),
    ("build-and-publish-app.yaml", "build", "Build and push arch image"),
    ("build-and-scan.yaml", "build", "build-image"),
    ("build-and-scan.yaml", "build", "build-image-retry"),
]
SITE_IDS = [f"{wf}:{job}:{loc}" for wf, job, loc in SITES]
JOBS = sorted({(wf, job) for wf, job, _ in SITES})
JOB_IDS = [f"{wf}:{job}" for wf, job in JOBS]

GIT_TOKEN_SECRET = re.compile(r"^\s*git_token=(.*)$", re.MULTILINE)


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _map(value: Any) -> dict[str, Any]:
    """``value`` if it is a mapping, else empty (YAML ``key:`` with no value)."""
    return value if isinstance(value, dict) else {}


def _steps(workflow_file: str, job: str) -> list[Step]:
    return _load(WORKFLOWS / workflow_file)["jobs"][job]["steps"]


def _locator(step: Step) -> str:
    return step.get("id") or step.get("name") or step.get("uses", "")


def _token_values(step: Step) -> list[str]:
    """Every value this step hands an image build as its git token."""
    with_ = _map(step.get("with"))
    values = GIT_TOKEN_SECRET.findall(str(with_.get("secrets", "")))
    if "git-token" in with_:
        values.append(str(with_["git-token"]))
    return values


def _yaml_files() -> list[Path]:
    return sorted(p for p in GITHUB.rglob("*") if p.suffix in {".yaml", ".yml"})


def _all_steps(doc: Any) -> list[Step]:
    """Steps of a workflow's jobs, or of a composite action's ``runs``."""
    steps: list[Step] = []
    for job in _map(_map(doc).get("jobs")).values():
        steps.extend(_map(job).get("steps") or [])
    steps.extend(_map(_map(doc).get("runs")).get("steps") or [])
    return steps


# ── No PAT anywhere ───────────────────────────────────────────────────────────


def test_no_git_token_reads_the_org_pat() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {_locator(step)}"
        for path in _yaml_files()
        for step in _all_steps(_load(path))
        for value in _token_values(step)
        if "ORG_PAT_GITHUB" in value
    ]
    assert offenders == [], "the image build's git token must not be the org PAT"


def test_every_workflow_site_is_pinned() -> None:
    """A new image build that passes a git token must be added to SITES."""
    found = sorted(
        (path.name, job_id, _locator(step))
        for path in sorted(WORKFLOWS.iterdir())
        if path.suffix in {".yaml", ".yml"}
        for job_id, job in _map(_load(path).get("jobs")).items()
        for step in _map(job).get("steps") or []
        if _token_values(step)
    )
    assert found == sorted(SITES)


# ── Each site reads the mint ──────────────────────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job", "locator"), SITES, ids=SITE_IDS)
def test_site_reads_the_app_mint(workflow_file: str, job: str, locator: str) -> None:
    steps = _steps(workflow_file, job)
    site = next(s for s in steps if _locator(s) == locator)
    assert _token_values(site) == [f"${{{{ {_token_ref(workflow_file, job)} }}}}"]
    mint_id = _mint_id(workflow_file, job)
    mint = next(i for i, s in enumerate(steps) if s.get("id") == mint_id)
    assert mint < steps.index(site), "the mint must run before the build"


@pytest.mark.parametrize(("workflow_file", "job"), JOBS, ids=JOB_IDS)
def test_mint_is_scoped_to_the_apps_private_deps(workflow_file: str, job: str) -> None:
    """The Dockerfile (caller code) reads this token, so it is scoped to the
    private repos the app names, and nothing is minted when it names none.
    Not gated on an opt-in flag: build-and-scan has none, and callers of the
    others build private-dep images without setting it. Only "is there a
    build at all" may gate it."""
    steps = _steps(workflow_file, job)
    mint_id = _mint_id(workflow_file, job)
    cond = next(s for s in steps if s.get("id") == mint_id).get("if")
    assert cond in (None, "inputs.image == ''")
    if (workflow_file, job) in SHARED_MINT:
        require = "${{ inputs.private-git-deps }}"
    else:
        assert (workflow_file, job, mint_id) in NOT_REQUIRED
        require = "false"
    assert_scoped_mint(steps, mint_id=mint_id, cond=cond, require=require)


@pytest.mark.parametrize(("workflow_file", "job"), JOBS, ids=JOB_IDS)
def test_token_reaches_only_the_build(workflow_file: str, job: str) -> None:
    sites = {loc for wf, j, loc in SITES if (wf, j) == (workflow_file, job)}
    token_ref = _token_ref(workflow_file, job)
    for step in _steps(workflow_file, job):
        if token_ref not in str({k: v for k, v in step.items() if k != "id"}):
            continue
        assert _locator(step) in sites, f"{_locator(step)} reads the image token"
        with_ = dict(_map(step.get("with")))
        with_.pop("git-token", None)
        with_.pop("secrets", None)
        if (workflow_file, job) in SHARED_MINT:
            # The host-side install (test_private_git_deps_wiring.py).
            with_.pop("git-auth-token", None)
        assert token_ref not in str(with_), _locator(step)
        assert token_ref not in str(_map(step.get("env"))), _locator(step)
        assert token_ref not in step.get("run", ""), _locator(step)


# ── GHCR keeps the PAT; interface unchanged ───────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job", "locator"), SITES, ids=SITE_IDS)
def test_ghcr_token_stays_on_the_pat(
    workflow_file: str, job: str, locator: str
) -> None:
    site = next(s for s in _steps(workflow_file, job) if _locator(s) == locator)
    ghcr = _map(site.get("with")).get("ghcr-token")
    if ghcr is not None:
        assert ghcr == "${{ secrets.ORG_PAT_GITHUB }}"


@pytest.mark.parametrize(
    "workflow_file", sorted({wf for wf, _, _ in SITES}), ids=lambda w: w
)
def test_secrets_interface_unchanged(workflow_file: str) -> None:
    on = _load(WORKFLOWS / workflow_file)[True]  # YAML 1.1 reads `on:` as True
    secrets = on["workflow_call"]["secrets"]
    # Still declared: GHCR uses it, and a caller mapping it would fail at startup.
    assert "ORG_PAT_GITHUB" in secrets
    for name in ("FLEET_APP_ID", "FLEET_APP_PRIVATE_KEY"):
        assert secrets[name]["required"] is False

"""Wiring of the ``git_token`` BuildKit secret in the image builds (FND-4744).

A Dockerfile whose ``uv sync`` clones a private atlanhq git dependency reads a
GitHub token from the ``git_token`` build secret. The image builds used to pass
the org-admin ORG_PAT_GITHUB there. Inside the build that token is readable by
every build backend the ``uv sync`` runs, including third-party setup scripts.

The builds now mint a read-only fleet App token for it. These tests pin that
shape:

* no ``git_token=`` secret or ``git-token:`` input under ``.github/`` reads
  ORG_PAT_GITHUB, and every one in a workflow reads the job's image mint;
* the mint is a read-only, org-wide App token from the FLEET secrets, with no
  opt-in gate (a Dockerfile without the mount ignores the secret, and several
  callers of these builds set no opt-in flag);
* the minted token reaches nothing but the build secret;
* GHCR login and push stay on ORG_PAT_GITHUB, because fleet packages are
  org-level and the App only has repo-level Packages permission.

The runner-side token for ``uv sync`` (FND-4740) is pinned separately, in
test_private_git_deps_wiring.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

Step = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"

MINT_ID = "image-git-token"
TOKEN_REF = f"steps.{MINT_ID}.outputs.token"
TOKEN_EXPR = f"${{{{ {TOKEN_REF} }}}}"
APP_TOKEN_ACTION = "actions/create-github-app-token@"

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
    assert _token_values(site) == [TOKEN_EXPR]
    mint = next(i for i, s in enumerate(steps) if s.get("id") == MINT_ID)
    assert mint < steps.index(site), "the mint must run before the build"


@pytest.mark.parametrize(("workflow_file", "job"), JOBS, ids=JOB_IDS)
def test_mint_is_read_only_app_token(workflow_file: str, job: str) -> None:
    mints = [s for s in _steps(workflow_file, job) if s.get("id") == MINT_ID]
    assert len(mints) == 1
    mint = mints[0]
    assert mint["uses"].startswith(APP_TOKEN_ACTION)
    # A failed mint leaves the secret empty: build-push-action warns and builds
    # without it, so only a Dockerfile that needs it fails, at its clone.
    assert mint.get("continue-on-error") is True
    # Not gated on an opt-in flag: build-and-scan has none, and callers of the
    # others build private-dep images without setting it. Only "is there a
    # build at all" may gate it.
    assert mint.get("if") in (None, "inputs.image == ''")
    with_ = mint["with"]
    assert with_["permission-contents"] == "read"
    assert with_["owner"] == "atlanhq"
    # Org-wide: the dependency repo varies per app.
    assert "repositories" not in with_
    assert "FLEET_APP_ID" in with_["app-id"]
    assert "FLEET_APP_PRIVATE_KEY" in with_["private-key"]


@pytest.mark.parametrize(("workflow_file", "job"), JOBS, ids=JOB_IDS)
def test_token_reaches_only_the_build(workflow_file: str, job: str) -> None:
    sites = {loc for wf, j, loc in SITES if (wf, j) == (workflow_file, job)}
    for step in _steps(workflow_file, job):
        if TOKEN_REF not in str({k: v for k, v in step.items() if k != "id"}):
            continue
        assert _locator(step) in sites, f"{_locator(step)} reads the image token"
        with_ = dict(_map(step.get("with")))
        with_.pop("git-token", None)
        with_.pop("secrets", None)
        assert TOKEN_REF not in str(with_), _locator(step)
        assert TOKEN_REF not in str(_map(step.get("env"))), _locator(step)
        assert TOKEN_REF not in step.get("run", ""), _locator(step)


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

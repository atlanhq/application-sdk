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
from pathlib import Path
from typing import Any

import pytest
import yaml
from _scoped_mint import NOT_REQUIRED, assert_scoped_mint
from _workflows import REPO_ROOT, WORKFLOWS, job, load_workflow, steps, triggers

Step = dict[str, Any]

GITHUB = REPO_ROOT / ".github"

MINT_ID = "image-git-token"
#: e2e mints once for the image build and the host-side ``uv sync`` (FND-4901).
SHARED_MINT = {("e2e-tests-reusable.yaml", "e2e"): "git-deps-token"}
#: The common-app-server e2e build (FND-5377) is the host's Dockerfile, not the
#: app's: its stage step clones every hosted app and application-sdk, which no
#: app's uv.lock names. So it is scoped by the host's own list
#: (common-app-server's scripts/token_repos.py), the one its PR image probe uses.
HOST_MINT = {("e2e-tests-reusable.yaml", "build-host-image"): "build-token"}


def _mint_id(workflow_file: str, job_id: str) -> str:
    key = (workflow_file, job_id)
    return SHARED_MINT.get(key) or HOST_MINT.get(key) or MINT_ID


def _token_ref(workflow_file: str, job_id: str) -> str:
    return f"steps.{_mint_id(workflow_file, job_id)}.outputs.token"


#: (workflow file, job id, step locator) for every step that hands a token to
#: an image build. The locator is the step's id, else its name.
SITES = [
    ("e2e-tests-reusable.yaml", "build-e2e-image", "build"),
    (
        "e2e-tests-reusable.yaml",
        "e2e",
        "atlanhq/application-sdk/.github/actions/sdr-e2e@main",
    ),
    ("build-and-publish-app.yaml", "build", "Build and push arch image"),
    ("build-and-scan.yaml", "build", "build-image"),
    ("build-and-scan.yaml", "build", "build-image-retry"),
    ("e2e-tests-reusable.yaml", "build-host-image", "Build and push the host image"),
]
SITE_IDS = [f"{wf}:{job}:{loc}" for wf, job, loc in SITES]
JOBS = sorted({(wf, job) for wf, job, _ in SITES})
JOB_IDS = [f"{wf}:{job}" for wf, job in JOBS]

GIT_TOKEN_SECRET = re.compile(r"^\s*git_token=(.*)$", re.MULTILINE)


def _map(value: Any) -> dict[str, Any]:
    """``value`` if it is a mapping, else empty (YAML ``key:`` with no value)."""
    return value if isinstance(value, dict) else {}


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
    found: list[Step] = []
    for body in _map(_map(doc).get("jobs")).values():
        found.extend(_map(body).get("steps") or [])
    found.extend(_map(_map(doc).get("runs")).get("steps") or [])
    return found


# ── No PAT anywhere ───────────────────────────────────────────────────────────


def test_no_git_token_reads_the_org_pat() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {_locator(step)}"
        for path in _yaml_files()
        for step in _all_steps(yaml.safe_load(path.read_text(encoding="utf-8")))
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
        for job_id, job in _map(load_workflow(path).get("jobs")).items()
        for step in _map(job).get("steps") or []
        if _token_values(step)
    )
    assert found == sorted(SITES)


# ── Each site reads the mint ──────────────────────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job_id", "locator"), SITES, ids=SITE_IDS)
def test_site_reads_the_app_mint(workflow_file: str, job_id: str, locator: str) -> None:
    job_steps = steps(job(load_workflow(workflow_file), job_id))
    site = next(s for s in job_steps if _locator(s) == locator)
    assert _token_values(site) == [f"${{{{ {_token_ref(workflow_file, job_id)} }}}}"]
    mint_id = _mint_id(workflow_file, job_id)
    mint = next(i for i, s in enumerate(job_steps) if s.get("id") == mint_id)
    assert mint < job_steps.index(site), "the mint must run before the build"


@pytest.mark.parametrize(("workflow_file", "job_id"), JOBS, ids=JOB_IDS)
def test_mint_is_scoped_to_the_apps_private_deps(
    workflow_file: str, job_id: str
) -> None:
    """The Dockerfile (caller code) reads this token, so it is scoped to the
    private repos the app names, and nothing is minted when it names none.
    Not gated on an opt-in flag: build-and-scan has none, and callers of the
    others build private-dep images without setting it. Only "is there a
    build at all" may gate it."""
    job_steps = steps(job(load_workflow(workflow_file), job_id))
    mint_id = _mint_id(workflow_file, job_id)
    if (workflow_file, job_id) in HOST_MINT:
        _assert_host_scoped_mint(job_steps, mint_id)
        return
    cond = next(s for s in job_steps if s.get("id") == mint_id).get("if")
    assert cond in (None, "inputs.image == ''")
    if (workflow_file, job_id) in SHARED_MINT:
        require = "${{ inputs.private-git-deps }}"
    else:
        assert (workflow_file, job_id, mint_id) in NOT_REQUIRED
        require = "false"
    assert_scoped_mint(job_steps, mint_id=mint_id, cond=cond, require=require)


def _assert_host_scoped_mint(job_steps: list[Step], mint_id: str) -> None:
    """Read-only, scoped to the host's own list, never unscoped."""
    ids = [s.get("id") for s in job_steps]
    mint = job_steps[ids.index(mint_id)]
    assert str(mint["uses"]).startswith("actions/create-github-app-token@")
    with_ = _map(mint.get("with"))
    assert with_["permission-contents"] == "read"
    assert with_["repositories"] == "${{ steps.build-repos.outputs.list }}"
    assert not any(
        k.startswith("permission-") and k != "permission-contents" for k in with_
    )
    lister = job_steps[ids.index("build-repos")]
    assert "scripts/token_repos.py" in lister["run"]
    assert "pipefail" in lister["run"], "a refused scope must fail, not mint unscoped"
    assert ids.index("build-repos") < ids.index(mint_id)


@pytest.mark.parametrize(("workflow_file", "job_id"), JOBS, ids=JOB_IDS)
def test_token_reaches_only_the_build(workflow_file: str, job_id: str) -> None:
    sites = {loc for wf, j, loc in SITES if (wf, j) == (workflow_file, job_id)}
    token_ref = _token_ref(workflow_file, job_id)
    for step in steps(job(load_workflow(workflow_file), job_id)):
        if token_ref not in str({k: v for k, v in step.items() if k != "id"}):
            continue
        assert _locator(step) in sites, f"{_locator(step)} reads the image token"
        with_ = dict(_map(step.get("with")))
        with_.pop("git-token", None)
        with_.pop("secrets", None)
        if (workflow_file, job_id) in SHARED_MINT:
            # The host-side install (test_private_git_deps_wiring.py).
            with_.pop("git-auth-token", None)
        assert token_ref not in str(with_), _locator(step)
        assert token_ref not in str(_map(step.get("env"))), _locator(step)
        assert token_ref not in step.get("run", ""), _locator(step)


# ── GHCR keeps the PAT; interface unchanged ───────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job_id", "locator"), SITES, ids=SITE_IDS)
def test_ghcr_token_stays_on_the_pat(
    workflow_file: str, job_id: str, locator: str
) -> None:
    site = next(
        s
        for s in steps(job(load_workflow(workflow_file), job_id))
        if _locator(s) == locator
    )
    ghcr = _map(site.get("with")).get("ghcr-token")
    if ghcr is not None:
        assert ghcr == "${{ secrets.ORG_PAT_GITHUB }}"


@pytest.mark.parametrize(
    "workflow_file", sorted({wf for wf, _, _ in SITES}), ids=lambda w: w
)
def test_secrets_interface_unchanged(workflow_file: str) -> None:
    secrets = triggers(load_workflow(workflow_file))["workflow_call"]["secrets"]
    # Still declared: GHCR uses it, and a caller mapping it would fail at startup.
    assert "ORG_PAT_GITHUB" in secrets
    for name in ("FLEET_APP_ID", "FLEET_APP_PRIVATE_KEY"):
        assert secrets[name]["required"] is False

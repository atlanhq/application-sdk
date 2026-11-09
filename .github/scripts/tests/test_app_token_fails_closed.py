"""A fleet App token that fails to mint fails the job (FND-4652).

The "Mint fleet App token" steps used to be ``continue-on-error: true``, and
every consumer read ``${{ steps.app-token.outputs.token ||
secrets.ORG_PAT_GITHUB }}``. A broken App credential then degraded silently to
the org-admin PAT: nothing went red, and the PAT could never be revoked because
nobody could tell which runs still depended on it.

These tests pin the fail-closed shape:

* no expression under ``.github/`` combines a minted token with
  ``ORG_PAT_GITHUB``, in either order;
* a ``create-github-app-token`` step may be ``continue-on-error`` only if it is
  on the allowlist below, each entry a soft mint whose consumer is guarded
  without any PAT fallback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"

FILES = sorted(
    [*GITHUB.glob("workflows/*.yml"), *GITHUB.glob("workflows/*.yaml")]
    + [*GITHUB.glob("actions/*/action.yml"), *GITHUB.glob("actions/*/action.yaml")]
)

EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)

#: Soft mints that are allowed to stay continue-on-error. None of them falls
#: back to ORG_PAT_GITHUB:
#: * git-deps-token / image-git-token: a failed mint leaves the token empty and
#:   the install or image build that needs it fails visibly (FND-4740/4744).
#: * stale.yml app-token: falls back to the repo's own GITHUB_TOKEN, a narrower
#:   credential, for a housekeeping job.
SOFT_MINT_IDS = {"git-deps-token", "image-git-token"}
SOFT_MINT_SITES = {("stale.yml", "app-token")}


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    if "jobs" in doc:
        jobs: dict[str, dict[str, Any] | None] = doc["jobs"] or {}
        steps: list[dict[str, Any]] = []
        for job in jobs.values():
            steps.extend((job or {}).get("steps") or [])
        return steps
    runs: dict[str, Any] = doc.get("runs") or {}
    return list(runs.get("steps") or [])


def org_pat_fallbacks(text: str) -> list[str]:
    """Expressions that combine a minted token with ORG_PAT_GITHUB."""
    return [
        expr.strip()
        for expr in EXPR.findall(text)
        if "outputs.token" in expr and "ORG_PAT_GITHUB" in expr
    ]


def soft_mints(name: str, doc: dict[str, Any]) -> list[str]:
    """Ids of continue-on-error App-token mints not on the allowlist."""
    return [
        str(step.get("id"))
        for step in _steps(doc)
        if "create-github-app-token" in str(step.get("uses", ""))
        and step.get("continue-on-error")
        and step.get("id") not in SOFT_MINT_IDS
        and (name, step.get("id")) not in SOFT_MINT_SITES
    ]


def test_files_found() -> None:
    assert len(FILES) > 20


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_org_pat_fallback_for_a_minted_token(path: Path) -> None:
    found = org_pat_fallbacks(path.read_text())
    assert not found, (
        f"{path.name}: a minted App token falls back to ORG_PAT_GITHUB: {found}. "
        "Let the mint fail the job instead (FND-4652)."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_app_token_mint_is_not_continue_on_error(path: Path) -> None:
    found = soft_mints(path.name, yaml.safe_load(path.read_text()))
    assert not found, (
        f"{path.name}: App-token mint(s) {found} are continue-on-error, so a "
        "broken App credential is invisible. Drop continue-on-error (FND-4652)."
    )


def test_detects_the_pre_fnd_4652_shape() -> None:
    """Red-green: the shape this change removed is caught by both checks."""
    text = """
jobs:
  j:
    steps:
      - name: Mint fleet App token
        id: app-token
        continue-on-error: true
        uses: actions/create-github-app-token@v3
      - uses: actions/checkout@v7
        with:
          token: ${{ steps.app-token.outputs.token || secrets.ORG_PAT_GITHUB }}
      - run: echo
        env:
          GH_TOKEN: ${{ secrets.ORG_PAT_GITHUB || steps.app-token.outputs.token }}
"""
    assert len(org_pat_fallbacks(text)) == 2
    assert soft_mints("x.yaml", yaml.safe_load(text)) == ["app-token"]

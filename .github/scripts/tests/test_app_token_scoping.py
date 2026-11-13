"""Every fleet App-token mint is scoped, or is a reviewed exemption (FND-4902).

``actions/create-github-app-token`` grants, by default, every permission the
App holds; with ``owner`` set and no ``repositories``, it grants them on every
repository in the installation. A read-only reporter minted that way holds the
same all-permission, fleet-wide token as the Renovate runner, and nothing
stopped the next copy.

Two rules, each with its own exemption table:

* **Permissions.** Every mint sets at least one ``permission-*`` input.
  ``UNSCOPED_PERMISSIONS`` is the baseline that predates this guard. It may only
  shrink: scoping a mint without removing its entry fails
  :func:`test_exemptions_are_not_stale`, and a new unscoped mint fails
  :func:`test_mint_requests_permissions`.
* **Repositories.** A mint that sets ``owner`` sets ``repositories``.
  (``owner`` and ``repositories`` both unset scopes the token to the current
  repository, which is already narrow.) ``OWNER_WIDE`` lists the mints that
  need fleet reach, each with the reason a reviewer accepted.

``READ_ONLY_FLEET`` pins the permission sets of the owner-wide dashboards that
only read the fleet. An under-scoped token fails at the first API call, so each
set is exactly what the job's scripts call; the workflow comments name the
endpoint behind every permission.

Keys are ``(file, job, step id)``: step ids repeat across and within files.
"""

from __future__ import annotations

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

APP_TOKEN_ACTION = "actions/create-github-app-token@"

Key = tuple[str, str, str]
Step = dict[str, Any]

#: Mints with no ``permission-*`` input when this guard landed. Shrink only:
#: scope the mint (the narrowest permissions its consumers call), then delete
#: its line here.
UNSCOPED_PERMISSIONS: frozenset[Key] = frozenset(
    {
        ("build-and-publish-app.yaml", "leak-scan", "app-token"),
        ("build-and-publish-app.yaml", "app-deployment-dispatcher", "app-token"),
        ("build-and-scan.yaml", "security-gate", "app-token"),
        ("capability-manifest-regen.yaml", "regen", "app-token"),
        ("check-dapr-version.yaml", "sync", "fleet-token"),
        ("conformance-publish.yml", "fan-out", "app-token"),
        ("conformance-release.yml", "open-release-pr", "app-token"),
        ("conformance-resync.yml", "resync", "app-token"),
        ("contract-toolkit-publish.yml", "fan-out", "app-token"),
        ("contract-toolkit-release.yml", "open-release-pr", "app-token"),
        ("e2e-callback-watchdog.yml", "sweep", "checks-app-token"),
        ("harbor-release.yaml", "build", "app-token"),
        ("pull_request.yaml", "trivy-container", "app-token"),
        ("pull_request.yaml", "build-sdk-base-image", "app-token"),
        ("pull_request.yaml", "connector-tests", "app-token"),
        ("pull_request.yaml", "connector-tests", "checks-app-token"),
        ("pull_request.yaml", "sdr-k8s-e2e", "app-token"),
        ("release-version-bump.yaml", "bump-version", "app-token"),
        ("release-version-bump.yaml", "publish", "app-token"),
        ("release.yaml", "bump-version", "app-token"),
        ("renovate-dashboard.yaml", "renovate-dashboard", "app-token"),
        ("renovate-rebase-conflicted.yaml", "rebase", "app-token"),
        ("renovate.yaml", "discover", "app-token"),
        ("renovate.yaml", "renovate", "app-token"),
        ("review-approval-reconcile.yml", "reconcile", "app-token"),
        ("schedule.yaml", "stale", "app-token"),
        ("sdk-tests-reusable.yaml", "unit", "app-token"),
        ("stale.yml", "stale", "app-token"),
        ("tag-and-publish.yaml", "release", "app-token"),
        ("tag-and-publish.yaml", "fan-out", "app-token"),
        ("tag-and-release.yaml", "create-release", "app-token"),
        ("tests-reusable.yaml", "report-to-sdk", "app-token"),
        ("trivy-container.yaml", "build", "app-token"),
        ("vuln-reconcile-on-release.yml", "reconcile", "fleet-token"),
    }
)

#: Mints with ``owner`` and no ``repositories``: the token reaches every repo
#: in the installation. Each needs fleet reach it can only learn at run time.
OWNER_WIDE: dict[Key, str] = {
    ("actions-cost-report.yaml", "report", "app-token"): (
        "reads Actions runs on a sample of fleet repos discovered at run time"
    ),
    ("gate-enforcement-dashboard.yaml", "gate-enforcement", "app-token"): (
        "reads rulesets and PR check rollups on every fleet repo"
    ),
    ("update-fleet-dashboards.yaml", "fleet-dashboards", "app-token"): (
        "reads Actions artifacts on every fleet repo"
    ),
    ("prune-dashboard-repo.yaml", "prune", "app-token"): (
        "asks whether each stored dashboard repo is archived, and builds the "
        "fleet roster"
    ),
    ("renovate-dashboard.yaml", "renovate-dashboard", "app-token"): (
        "scans Renovate PRs fleet-wide and re-arms their auto-merge"
    ),
    ("renovate.yaml", "discover", "app-token"): (
        "discovers the repos self-hosted Renovate runs on"
    ),
    ("renovate.yaml", "renovate", "app-token"): (
        "self-hosted Renovate opens and updates PRs across the fleet"
    ),
    ("renovate-rebase-conflicted.yaml", "rebase", "app-token"): (
        "rebases conflicted Renovate PRs across the fleet"
    ),
    ("conformance-publish.yml", "fan-out", "app-token"): (
        "fans a conformance release out to every consumer repo"
    ),
    ("conformance-resync.yml", "resync", "app-token"): (
        "opens bootstrap resync PRs across the fleet"
    ),
    ("contract-toolkit-publish.yml", "fan-out", "app-token"): (
        "fans a contract-toolkit release out to every consumer repo"
    ),
    ("tag-and-publish.yaml", "fan-out", "app-token"): (
        "fans an SDK release out to every consumer repo"
    ),
}

#: Owner-wide dashboards that only read the fleet: their exact permissions.
#: Each value is ``read``; metadata read is always granted and not listed.
READ_ONLY_FLEET: dict[Key, frozenset[str]] = {
    ("actions-cost-report.yaml", "report", "app-token"): frozenset(
        {"actions", "checks", "contents"}
    ),
    ("gate-enforcement-dashboard.yaml", "gate-enforcement", "app-token"): frozenset(
        {"actions", "checks", "contents", "pull-requests", "statuses"}
    ),
    ("update-fleet-dashboards.yaml", "fleet-dashboards", "app-token"): frozenset(
        {"actions", "contents"}
    ),
    ("prune-dashboard-repo.yaml", "prune", "app-token"): frozenset({"contents"}),
}


def _with(step: Step) -> dict[str, Any]:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def mints(name: str, doc: dict[str, Any]) -> dict[Key, Step]:
    """Every App-token mint in one workflow or composite action, by key."""
    jobs: dict[str, list[Step]]
    if "jobs" in doc:
        raw_jobs: dict[str, dict[str, Any] | None] = doc["jobs"] or {}
        jobs = {
            str(job_id): list((job or {}).get("steps") or [])
            for job_id, job in raw_jobs.items()
        }
    else:
        runs: dict[str, Any] = doc.get("runs") or {}
        jobs = {"runs": list(runs.get("steps") or [])}
    found: dict[Key, Step] = {}
    for job_id, steps in jobs.items():
        for step in steps:
            if not str(step.get("uses", "")).startswith(APP_TOKEN_ACTION):
                continue
            key = (name, job_id, str(step.get("id")))
            assert key not in found, f"two mints share the key {key}"
            found[key] = step
    return found


def permissions(step: Step) -> dict[str, str]:
    """``permission-*`` inputs, keyed by the permission name."""
    return {
        k.removeprefix("permission-"): str(v)
        for k, v in _with(step).items()
        if k.startswith("permission-")
    }


def is_owner_wide(step: Step) -> bool:
    """``owner`` set, ``repositories`` unset or empty: the whole installation."""
    w = _with(step)
    return bool(w.get("owner")) and not str(w.get("repositories") or "").strip()


def _all_mints() -> dict[Key, Step]:
    found: dict[Key, Step] = {}
    for path in FILES:
        found.update(mints(path.name, yaml.safe_load(path.read_text())))
    return found


ALL = _all_mints()


def test_mints_found() -> None:
    assert len(ALL) > 40


@pytest.mark.parametrize("key", sorted(ALL), ids=lambda k: ":".join(k))
def test_mint_requests_permissions(key: Key) -> None:
    if key in UNSCOPED_PERMISSIONS:
        pytest.skip("pre-FND-4902 baseline")
    assert permissions(ALL[key]), (
        f"{key}: the mint sets no permission-* input, so the token carries "
        "every permission the App holds. Request only what its consumers call "
        "(FND-4902)."
    )


@pytest.mark.parametrize("key", sorted(ALL), ids=lambda k: ":".join(k))
def test_owner_mint_names_repositories(key: Key) -> None:
    if key in OWNER_WIDE:
        pytest.skip(OWNER_WIDE[key])
    assert not is_owner_wide(ALL[key]), (
        f"{key}: owner is set and repositories is not, so the token reaches "
        "every repo in the installation. Name the repositories, or add a "
        "reviewed OWNER_WIDE entry saying why it needs the fleet (FND-4902)."
    )


def test_exemptions_are_not_stale() -> None:
    """Every exemption names a mint that still needs it; the tables only shrink."""
    stale_permissions = sorted(
        k for k in UNSCOPED_PERMISSIONS if k not in ALL or permissions(ALL[k])
    )
    stale_owner_wide = sorted(
        k for k in OWNER_WIDE if k not in ALL or not is_owner_wide(ALL[k])
    )
    assert not stale_permissions, (
        f"UNSCOPED_PERMISSIONS entries no longer needed: {stale_permissions}"
    )
    assert not stale_owner_wide, (
        f"OWNER_WIDE entries no longer needed: {stale_owner_wide}"
    )


@pytest.mark.parametrize("key", sorted(READ_ONLY_FLEET), ids=lambda k: ":".join(k))
def test_read_only_fleet_dashboard_is_read_scoped(key: Key) -> None:
    assert key in ALL, f"{key}: mint not found"
    assert key in OWNER_WIDE
    granted = permissions(ALL[key])
    assert set(granted) == READ_ONLY_FLEET[key]
    assert set(granted.values()) == {"read"}, f"{key}: {granted}"


def test_detects_unscoped_and_owner_wide_mints() -> None:
    """Red-green: the shapes this guard exists for are caught; scoped ones pass."""
    text = """
jobs:
  j:
    steps:
      - id: bare
        uses: actions/create-github-app-token@v3
        with: {app-id: x, private-key: y, owner: atlanhq}
      - id: blank-repos
        uses: actions/create-github-app-token@v3
        with: {app-id: x, private-key: y, owner: atlanhq, repositories: " "}
      - id: current-repo
        uses: actions/create-github-app-token@v3
        with: {app-id: x, private-key: y, permission-contents: read}
      - id: scoped
        uses: actions/create-github-app-token@v3
        with:
          app-id: x
          private-key: y
          owner: atlanhq
          repositories: application-sdk
          permission-contents: read
      - id: not-a-mint
        uses: actions/checkout@v7
"""
    found = mints("x.yaml", yaml.safe_load(text))
    assert sorted(k[2] for k in found) == [
        "bare",
        "blank-repos",
        "current-repo",
        "scoped",
    ]
    by_id = {k[2]: step for k, step in found.items()}
    assert [i for i, s in by_id.items() if not permissions(s)] == [
        "bare",
        "blank-repos",
    ]
    assert [i for i, s in by_id.items() if is_owner_wide(s)] == [
        "bare",
        "blank-repos",
    ]
    assert permissions(by_id["scoped"]) == {"contents": "read"}

"""Wiring of a hosted api app's e2e on the e2e-only common-app-server (FND-5377).

Pins the order and gating that the scripts cannot check on their own: the
shared host lease comes after the app's lease and is always released, every
host step runs only for a hosted app, and the host lease is confirmed before
the host is installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github/workflows/e2e-tests-reusable.yaml"
)
HOSTED = "needs.prepare-host-image.outputs.hosted == 'true'"

#: (job, step name) for every step that only a hosted app's run takes.
HOST_STEPS = [
    ("lease-tenant", "Mint the common-app-server lease token"),
    ("lease-tenant", "Mint a token to read the hosted apps' runs"),
    ("lease-tenant", "Acquire the common-app-server leases"),
    ("prepare-tenant", "Mint a read token for common-app-server"),
    ("prepare-tenant", "Confirm this run holds the common-app-server lease"),
    ("prepare-tenant", "Require the host deploy config"),
    ("prepare-tenant", "Install the common-app-server build as the e2e host"),
    ("e2e", "Verify the tenant runs this run's common-app-server build"),
    ("release-tenant", "Mint the common-app-server lease token"),
    ("release-tenant", "Release the common-app-server lease"),
]


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _steps(jobs: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return jobs[job]["steps"]


def _index(jobs: dict[str, Any], job: str, name: str) -> int:
    names = [step.get("name") for step in _steps(jobs, job)]
    assert names.count(name) == 1, f"{job}: expected one step named {name!r}"
    return names.index(name)


def _step(jobs: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    return _steps(jobs, job)[_index(jobs, job, name)]


@pytest.mark.parametrize(
    ("job", "name"), HOST_STEPS, ids=[f"{j}:{n}" for j, n in HOST_STEPS]
)
def test_every_host_step_runs_only_for_a_hosted_app(
    jobs: dict[str, Any], job: str, name: str
) -> None:
    assert HOSTED in str(_step(jobs, job, name).get("if", ""))


def test_the_host_lease_is_taken_after_the_apps_own(jobs: dict[str, Any]) -> None:
    # Every run takes the two in the same order, so two runs cannot each hold
    # one and wait for the other.
    assert _index(jobs, "lease-tenant", "Acquire the tenant leases") < _index(
        jobs, "lease-tenant", "Acquire the common-app-server leases"
    )


def test_the_host_lease_is_released_however_the_legs_ended(
    jobs: dict[str, Any],
) -> None:
    for name in (
        "Mint the common-app-server lease token",
        "Release the common-app-server lease",
    ):
        assert "always()" in str(_step(jobs, "release-tenant", name).get("if", "")), (
            name
        )
    assert "prepare-host-image" in jobs["release-tenant"]["needs"]


def test_the_host_lease_is_confirmed_before_the_host_is_installed(
    jobs: dict[str, Any],
) -> None:
    confirm = _index(
        jobs, "prepare-tenant", "Confirm this run holds the common-app-server lease"
    )
    guard = _index(jobs, "prepare-tenant", "Require the host deploy config")
    install = _index(
        jobs, "prepare-tenant", "Install the common-app-server build as the e2e host"
    )
    app = _index(jobs, "prepare-tenant", "Install the app under test")
    assert confirm < guard < install < app


def test_the_host_is_installed_as_the_e2e_host_on_records_only(
    jobs: dict[str, Any],
) -> None:
    step = _step(
        jobs, "prepare-tenant", "Install the common-app-server build as the e2e host"
    )
    run = step["run"]
    assert '--app-id "$HOST_APP_ID"' in run
    assert "--records-only true" in run
    assert (
        step["env"]["HOST_APP_ID"]
        == "${{ needs.prepare-host-image.outputs.host-app-id }}"
    )


def test_the_app_is_routed_to_the_e2e_host_only_when_hosted(
    jobs: dict[str, Any],
) -> None:
    step = _step(jobs, "prepare-tenant", "Install the app under test")
    assert '--route-to-common-host "$ROUTE_TO_COMMON_HOST"' in step["run"]
    assert step["env"]["ROUTE_TO_COMMON_HOST"] == (
        "${{ needs.prepare-host-image.outputs.hosted == 'true' && 'true' || 'false' }}"
    )


def test_the_host_image_jobs_run_only_for_a_hosted_app(jobs: dict[str, Any]) -> None:
    assert (
        "needs.prepare-host-image.outputs.hosted == 'true'"
        in jobs["build-host-image"]["if"]
    )
    assert (
        "needs.build-host-image.result == 'success'" in jobs["merge-host-image"]["if"]
    )


def test_the_build_refuses_a_missing_overrides_file(jobs: dict[str, Any]) -> None:
    names = [step.get("name") for step in _steps(jobs, "build-host-image")]
    require = names.index("Require the prepared overrides file")
    assert names.index("Lay the prepared host files over it") < require
    assert require < names.index("Build and push the host image")
    assert (
        "hosted-api-apps.overrides.toml"
        in _steps(jobs, "build-host-image")[require]["run"]
    )


def test_the_e2e_host_id_is_checked_against_the_shared_host(
    jobs: dict[str, Any],
) -> None:
    run = _step(jobs, "prepare-host-image", "Read the e2e host's app id")["run"]
    assert "--file common-app-server/e2e-host.toml" in run
    assert "--shared-atlan-yaml common-app-server/atlan.yaml" in run

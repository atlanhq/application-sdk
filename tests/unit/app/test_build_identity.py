"""Tests for application_sdk.app.build_identity (FND-1684).

The build identity is the one fact a running pod can report that distinguishes
the image built by a given CI run from an image built months ago. Everything
else an app already exposes — the declared semver, the served manifest, the
generated configmaps — is a function of committed source, so it is identical
across every build of that source.

The e2e version check used to have no such source to read, and read the
marketplace install record instead: the same record the install writes and then
skips on. Once that record existed for a build, the check could only agree with
it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.build_identity import (
    BUILD_ID_ENV,
    BUILD_IDENTITY_CONFIGMAP_ID,
    BUILD_INFO_BUILD_ID_KEY,
    build_identity,
)
from application_sdk.contracts.base import Input, Output


def test_build_identity_reads_the_image_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BUILD_ID_ENV, "sdr-test-abc12345")
    assert build_identity() == "sdr-test-abc12345"


def test_an_unstamped_image_reports_empty_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image built by hand or by an older CI must still start.

    The stamp is added by Atlan CI, not by any connector's own Dockerfile, so
    "no build identity" is a normal state and every reader treats it as "cannot
    answer", never as a mismatch.
    """
    monkeypatch.delenv(BUILD_ID_ENV, raising=False)
    assert build_identity() == ""


def test_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamp is compared for exact equality against an image tag.

    An ENV that picked up a trailing newline from a shell would otherwise turn
    a correctly reconciled tenant into a loud version mismatch.
    """
    monkeypatch.setenv(BUILD_ID_ENV, "  sdr-test-abc12345\n")
    assert build_identity() == "sdr-test-abc12345"


def test_the_value_is_not_frozen_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read per call, not cached.

    In production the value is an image ENV and never changes, so a cache would
    be safe there and misleading everywhere else — tests and local runs set it
    per case, and a module-level snapshot would freeze whichever value happened
    to exist at first import.
    """
    monkeypatch.setenv(BUILD_ID_ENV, "first")
    assert build_identity() == "first"
    monkeypatch.setenv(BUILD_ID_ENV, "second")
    assert build_identity() == "second"


@dataclass
class _StampInput(Input):
    value: str = ""


@dataclass
class _StampOutput(Output):
    result: str = ""


def test_registration_gives_every_app_the_build_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base-level hook: no per-app change, and no per-app way to miss it."""
    monkeypatch.setenv(BUILD_ID_ENV, "sdr-test-abc12345")

    from application_sdk.app import App

    class StampedApp(App):
        async def run(self, input: _StampInput) -> _StampOutput:
            return _StampOutput(result=input.value)

    assert StampedApp._app_build_id == "sdr-test-abc12345"
    assert StampedApp.get_build_id(StampedApp) == "sdr-test-abc12345"  # type: ignore[arg-type]


def test_an_app_registered_without_a_stamp_reports_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(BUILD_ID_ENV, raising=False)

    from application_sdk.app import App

    class UnstampedApp(App):
        async def run(self, input: _StampInput) -> _StampOutput:
            return _StampOutput(result=input.value)

    assert UnstampedApp._app_build_id == ""


def test_app_context_carries_it_separately_from_app_version() -> None:
    """They are not interchangeable, and conflating them is the original defect.

    ``app_version`` is declared in the app's source, so it is the same for every
    build of that source — which is exactly why it could not answer "is this pod
    running the build under test?".
    """
    from application_sdk.app.context import AppContext

    context = AppContext(
        app_name="app", app_version="1.0.0", build_id="sdr-test-abc12345"
    )
    assert context.app_version == "1.0.0"
    assert context.build_id == "sdr-test-abc12345"


def test_app_context_build_id_defaults_to_empty() -> None:
    """Defaulted so no existing construction site had to change."""
    from application_sdk.app.context import AppContext

    assert AppContext(app_name="app", app_version="1.0.0").build_id == ""


def test_the_reserved_configmap_id_is_not_a_plausible_generated_stem() -> None:
    """It is answered ahead of the generated-file scan, so it must not collide.

    A generated artifact is named for its entrypoint or its connector; this is
    named for the thing it reports, and carries the marketplace's own
    ``atlan-`` prefix so it reads as a platform id rather than an app's file.
    """
    assert BUILD_IDENTITY_CONFIGMAP_ID == "atlan-build-identity"


# ── The publish path's carrier: app/atlan_build.json ─────────────────────────
#
# .github/actions/build-app-image stamps ATLAN_BUILD_ID on the e2e image only.
# A released image is built by build-and-publish-app.yaml, which never calls
# that action, so it carries the same value in the baked identity file instead.


def test_a_released_image_reports_the_baked_build_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish path carries no ENV, so the file has to answer.

    Without this a released pod reports "" on the build-identity route, and the
    e2e check cannot distinguish it from a stale one — the exact hole FND-1684
    closed for the e2e path.
    """
    info = tmp_path / "atlan_build.json"
    info.write_text('{"build_id": "main-abc1234", "commit_sha": "abc1234def"}')
    monkeypatch.delenv(BUILD_ID_ENV, raising=False)
    monkeypatch.setenv("ATLAN_BUILD_INFO_PATH", str(info))

    assert build_identity() == "main-abc1234"


def test_the_stamped_env_wins_over_the_baked_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An e2e build reports exactly what it reported before this fallback.

    The e2e image is built from the same commit as a release could be, so both
    carriers can be present at once. The ENV is the value the e2e check derived
    and is comparing against, so it must be the one that answers.
    """
    info = tmp_path / "atlan_build.json"
    info.write_text('{"build_id": "main-abc1234"}')
    monkeypatch.setenv("ATLAN_BUILD_INFO_PATH", str(info))
    monkeypatch.setenv(BUILD_ID_ENV, "sdr-test-abc12345")

    assert build_identity() == "sdr-test-abc12345"


def test_an_empty_env_falls_through_rather_than_shadowing_the_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank ENV is "unset", not "no identity".

    A Helm chart that templates the var unconditionally sets it to "" on a
    deployment that has no value for it, and treating that as an answer would
    hide the file behind it.
    """
    info = tmp_path / "atlan_build.json"
    info.write_text('{"build_id": "main-abc1234"}')
    monkeypatch.setenv("ATLAN_BUILD_INFO_PATH", str(info))
    monkeypatch.setenv(BUILD_ID_ENV, "   ")

    assert build_identity() == "main-abc1234"


def test_neither_carrier_is_still_empty_not_an_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BUILD_ID_ENV, raising=False)
    monkeypatch.setenv("ATLAN_BUILD_INFO_PATH", str(tmp_path / "absent.json"))

    assert build_identity() == ""


def test_the_baked_key_matches_what_ci_writes() -> None:
    """Wiring guard: CI is the writer, this module is the reader.

    A divergent spelling degrades the check to "the pod reports no build
    identity" rather than failing loudly, which is the failure mode this repo
    already guards for ATLAN_BUILD_ID between the stamp script and BUILD_ID_ENV.
    """
    from pathlib import Path

    # Resolved from this file, not the CWD: the guard must read the repo's own
    # workflow wherever pytest was invoked from.
    repo = Path(__file__).resolve().parents[3]
    workflow = (repo / ".github/workflows/build-and-publish-app.yaml").read_text(
        encoding="utf-8"
    )
    bake = workflow.split("Bake build identity into the image", 1)
    assert len(bake) == 2, "the bake step was renamed; this guard reads it by name"
    step = bake[1].split("- name: Build and push arch image", 1)[0]
    assert f'"{BUILD_INFO_BUILD_ID_KEY}":' in step, (
        f"the bake step no longer writes a {BUILD_INFO_BUILD_ID_KEY!r} key, so "
        "every released image would silently report no build identity"
    )

"""Tests for application_sdk.app.build_identity (FND-1684).

The build identity is the one fact a running pod can report that distinguishes
the image built by a given CI run from an image built months ago. Everything
else an app already exposes — the declared semver, the served manifest, the
generated configmaps — is a function of committed source, so it is identical
across every build of that source.

The e2e version check used to have no such source to read, and read the
marketplace install record instead: the same record the install writes and then
skips on. A tenant whose pods had not moved in 44 days passed it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.build_identity import (
    BUILD_ID_ENV,
    BUILD_IDENTITY_CONFIGMAP_ID,
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

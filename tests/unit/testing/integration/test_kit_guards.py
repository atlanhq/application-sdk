"""The kit's local-scratch fixture and its two pre-run guards.

Both guards exist because the failure they catch is silent. A wrong
``ATLAN_APPLICATION_NAME`` writes a run's artifacts under one identity while the
run reports itself under another; a replaced infrastructure context sends the
run to a store the fixtures never expose. Neither raises on its own — the suite
just fails later, with "no files", pointing at the App instead of the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk import constants
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure
from application_sdk.testing.integration._errors import (
    AppNameMismatchError,
    InfrastructureReplacedError,
)
from application_sdk.testing.integration.fixtures import (
    APPLICATION_NAME_ENV,
    _verify_app_name,
    _verify_infrastructure,
)


class _App:
    """Stands in for an App class; only its ``__name__`` reaches the error."""


class TestVerifyAppName:
    def test_passes_when_the_env_matches_the_registered_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APPLICATION_NAME_ENV, "power-bi-app")

        _verify_app_name(_App, "power-bi-app")

    def test_raises_when_the_env_disagrees(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APPLICATION_NAME_ENV, "powerbi")

        with pytest.raises(AppNameMismatchError) as excinfo:
            _verify_app_name(_App, "power-bi-app")

        message = str(excinfo.value)
        assert "'powerbi'" in message
        assert "'power-bi-app'" in message

    def test_ignores_an_unset_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SDK default is ``"default"``; failing every suite that never set
        the variable is a different conversation, not this guard's job."""
        monkeypatch.delenv(APPLICATION_NAME_ENV, raising=False)

        _verify_app_name(_App, "power-bi-app")


@pytest.fixture(autouse=True)
def _restore_global_infrastructure():
    """These tests install contexts globally; put the previous one back."""
    from application_sdk.infrastructure.context import get_infrastructure

    previous = get_infrastructure()
    yield
    if previous is not None:
        set_infrastructure(previous)


class TestVerifyInfrastructure:
    def test_passes_when_the_installed_context_is_still_live(self) -> None:
        ctx = InfrastructureContext()
        set_infrastructure(ctx)

        _verify_infrastructure(ctx)

    def test_raises_when_something_replaced_the_context(self) -> None:
        installed = InfrastructureContext()
        set_infrastructure(installed)
        set_infrastructure(InfrastructureContext())

        with pytest.raises(InfrastructureReplacedError) as excinfo:
            _verify_infrastructure(installed)

        assert "set_infrastructure" in str(excinfo.value)

    def test_disabled_when_no_context_was_recorded(self) -> None:
        """An executor built outside the kit has nothing to compare against."""
        _verify_infrastructure(None)


class TestTemporaryPath:
    def test_redirects_the_constant_at_the_yielded_directory(
        self, temporary_path: Path
    ) -> None:
        assert constants.TEMPORARY_PATH == str(temporary_path)
        assert temporary_path.is_dir()

    def test_is_not_the_repo_relative_default(self, temporary_path: Path) -> None:
        """The point of the fixture: run files leave the working tree, and two
        runs of the same suite do not read each other's artifacts."""
        assert not str(temporary_path).startswith("./local")
        assert Path(constants.TEMPORARY_PATH).is_absolute()

"""The kit's local-scratch fixture and its two pre-run guards.

Both guards exist because the failure they catch is silent. A wrong
``ATLAN_APPLICATION_NAME`` writes a run's artifacts under one identity while the
run reports itself under another; a replaced infrastructure context sends the
run to a store the fixtures never expose. Neither raises on its own — the suite
just fails later, with "no files", pointing at the App instead of the fixture.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from application_sdk import constants
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure

# Imported at module top deliberately: collection binds this module's
# TEMPORARY_PATH before the session fixture runs, so the redirect tests below
# exercise the already-imported-binder case the fixture exists to cover.
from application_sdk.observability import utils as observability_utils
from application_sdk.testing.integration._errors import (
    AppNameMismatchError,
    InfrastructureReplacedError,
)
from application_sdk.testing.integration.fixtures import (
    APPLICATION_NAME_ENV,
    AppExecutor,
    _verify_app_name,
    _verify_infrastructure,
    _verify_registration,
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
    """These tests install contexts globally; put the previous state back —
    including the no-context state, so a test's context never bleeds into
    whatever unit test runs next in the session."""
    from application_sdk.infrastructure.context import (
        clear_infrastructure,
        get_infrastructure,
    )

    previous = get_infrastructure()
    yield
    if previous is None:
        clear_infrastructure()
    else:
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


class TestGuardsOnTheCallPath:
    """Each guard through the path an adopter actually runs — so removing a
    guard's call site (not just the guard) turns a test red."""

    @pytest.mark.asyncio
    async def test_execute_app_refuses_a_replaced_context(self) -> None:
        installed = InfrastructureContext()
        set_infrastructure(installed)
        executor = AppExecutor(
            backend=mock.MagicMock(), expected_infrastructure=installed
        )
        set_infrastructure(InfrastructureContext())

        with pytest.raises(InfrastructureReplacedError):
            await executor.execute_app(_App, object())

        executor.backend.execute.assert_not_called()

    def test_executor_fixture_wires_the_installed_context(self) -> None:
        """The fixture must hand the installed context to the executor —
        dropping the kwarg would silently disable the guard for every adopter."""
        from application_sdk.testing.integration import fixtures as kit_fixtures

        ctx = InfrastructureContext()
        built = kit_fixtures.executor.__wrapped__(
            temporal_client=mock.MagicMock(),
            worker=None,
            integration_task_queue="q",
            infrastructure=ctx,
        )

        assert built.expected_infrastructure is ctx

    def test_registration_path_raises_on_a_mismatched_env(
        self, clean_app_registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.contracts.base import Input, Output

        class _KitInput(Input):
            pass

        class _KitOutput(Output):
            pass

        class _RegisteredApp:
            _app_name = "power-bi-app"

        clean_app_registry.register(
            "power-bi-app", "1.0.0", _RegisteredApp, _KitInput, _KitOutput
        )
        monkeypatch.setenv(APPLICATION_NAME_ENV, "powerbi")

        with pytest.raises(AppNameMismatchError):
            _verify_registration(_RegisteredApp)


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

    def test_redirect_reaches_import_time_binders(self, temporary_path: Path) -> None:
        """Most consumers bind TEMPORARY_PATH at module import; the fixture must
        reach those bindings, not just the constants module, or the run writes
        ./local/tmp/ while the suite asserts on the temp dir."""
        assert observability_utils.TEMPORARY_PATH == str(temporary_path)

    def test_a_consumer_builds_paths_under_the_redirect(
        self, temporary_path: Path
    ) -> None:
        built = observability_utils.get_observability_dir()
        assert built.startswith(str(temporary_path))

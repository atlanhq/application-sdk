"""FND-4921: one composition of the run prefix, from the App that runs the task."""

from __future__ import annotations

from collections.abc import Iterator
from unittest import mock

import pytest

from application_sdk.app.registry import AppRegistry, _running_app_name
from application_sdk.constants import WORKFLOW_OUTPUT_PATH_TEMPLATE, _run_prefix
from application_sdk.execution._temporal import activity_utils


def test_run_prefix_fills_the_template() -> None:
    assert (
        _run_prefix("my-app", "wf", "run") == "artifacts/apps/my-app/workflows/wf/run"
    )
    assert _run_prefix("a", "b", "c") == WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name="a", workflow_id="b", run_id="c"
    )


class TestBuildOutputPath:
    @pytest.fixture(autouse=True)
    def _run(self) -> Iterator[None]:
        """No single registered App, so the resolver's last resort is the env name."""
        with (
            mock.patch.object(AppRegistry, "list_apps", return_value=[]),
            mock.patch("application_sdk.constants.APPLICATION_NAME", "env-app"),
            mock.patch.object(activity_utils, "get_workflow_id", return_value="wf"),
            mock.patch.object(
                activity_utils, "get_workflow_run_id", return_value="run"
            ),
        ):
            yield

    def test_the_running_app_wins_over_the_env_name(self) -> None:
        token = _running_app_name.set("my-app")
        try:
            assert (
                activity_utils.build_output_path()
                == "artifacts/apps/my-app/workflows/wf/run"
            )
            assert activity_utils.run_temp_dir().endswith(
                "artifacts/apps/my-app/workflows/wf/run"
            )
        finally:
            _running_app_name.reset(token)

    def test_without_a_running_app_falls_back_to_the_env_name(self) -> None:
        assert (
            activity_utils.build_output_path()
            == "artifacts/apps/env-app/workflows/wf/run"
        )

    def test_explicit_prefer_still_wins(self) -> None:
        token = _running_app_name.set("my-app")
        try:
            assert AppRegistry.resolve_running_app_name(prefer="other") == "other"
        finally:
            _running_app_name.reset(token)


def test_template_reexport_is_a_deprecated_alias() -> None:
    """The template is no longer imported here; the old import still works, warned."""
    assert "WORKFLOW_OUTPUT_PATH_TEMPLATE" not in vars(activity_utils)
    with pytest.warns(DeprecationWarning, match="WORKFLOW_OUTPUT_PATH_TEMPLATE"):
        value = activity_utils.WORKFLOW_OUTPUT_PATH_TEMPLATE
    assert value == WORKFLOW_OUTPUT_PATH_TEMPLATE

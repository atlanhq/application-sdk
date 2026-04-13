"""Unit tests for Temporal workflow utilities."""

from __future__ import annotations

from dataclasses import dataclass

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.workflows import get_all_app_workflows


@dataclass
class _WfInput(Input):
    value: str = ""


@dataclass
class _WfOutput(Output):
    result: str = ""


@dataclass
class _WfInput2(Input):
    value: str = ""


@dataclass
class _WfOutput2(Output):
    result: str = ""


class TestGetAllAppWorkflows:
    """Tests for get_all_app_workflows()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_empty_when_no_apps_registered(self) -> None:
        workflows = get_all_app_workflows()
        assert workflows == []

    def test_returns_temporal_workflow_class_for_registered_app(self) -> None:
        class AlphaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        assert len(workflows) == 1
        # The returned class should be a Temporal workflow definition
        assert hasattr(workflows[0], "__temporal_workflow_definition")

    def test_returns_all_registered_apps(self) -> None:
        class BetaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        class GammaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        # Two apps, one entry point each → two workflow classes
        assert len(workflows) == 2

    def test_returns_class_not_metadata(self) -> None:
        class DeltaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        for item in workflows:
            assert isinstance(item, type), f"Expected type, got {type(item)}"

    def test_returns_list_type(self) -> None:
        workflows = get_all_app_workflows()
        assert isinstance(workflows, list)

    def test_generated_workflow_has_correct_name(self) -> None:
        class EpsilonApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        assert len(workflows) == 1
        defn = getattr(workflows[0], "__temporal_workflow_definition")
        # Implicit single-entry-point uses just the app name (no colon)
        assert defn.name == "epsilon-app"

    def test_multi_entrypoint_app_returns_multiple_workflow_classes(self) -> None:
        from application_sdk.app.entrypoint import entrypoint

        # Use module-level types — locally-defined dataclasses can't be resolved
        # by get_type_hints() because 'from __future__ import annotations' is active.
        class MultiApp(App):
            @entrypoint
            async def extract_data(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

            @entrypoint
            async def mine_queries(self, input: _WfInput2) -> _WfOutput2:
                return _WfOutput2()

        workflows = get_all_app_workflows()
        assert len(workflows) == 2
        names = {getattr(wf, "__temporal_workflow_definition").name for wf in workflows}
        assert "multi-app:extract-data" in names
        assert "multi-app:mine-queries" in names

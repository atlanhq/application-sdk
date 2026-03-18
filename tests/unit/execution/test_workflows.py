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

    def test_returns_registered_app_class(self) -> None:
        class AlphaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        assert AlphaApp in workflows

    def test_returns_all_registered_apps(self) -> None:
        class BetaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        class GammaApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        workflows = get_all_app_workflows()
        assert BetaApp in workflows
        assert GammaApp in workflows

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

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


class TestGenerateWorkflowClassBehaviour:
    """Tests for generate_workflow_class() — runtime behaviour and idempotency."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        # Clear the cache so tests are independent
        from application_sdk.app.base import _workflow_class_cache

        _workflow_class_cache.clear()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        from application_sdk.app.base import _workflow_class_cache

        _workflow_class_cache.clear()

    def test_generate_is_idempotent(self) -> None:
        """Calling generate_workflow_class twice returns the same class object."""
        from application_sdk.app.base import generate_workflow_class

        class IdempotentApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        meta = AppRegistry.get_instance().get("idempotent-app")
        ep = meta.entry_points["run"]
        cls1 = generate_workflow_class(IdempotentApp, ep)
        cls2 = generate_workflow_class(IdempotentApp, ep)
        assert cls1 is cls2

    def test_generated_class_has_temporal_workflow_definition(self) -> None:
        """Generated class carries Temporal's __temporal_workflow_definition."""
        from application_sdk.app.base import generate_workflow_class

        class WfDefnApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        meta = AppRegistry.get_instance().get("wf-defn-app")
        ep = meta.entry_points["run"]
        wf_cls = generate_workflow_class(WfDefnApp, ep)
        assert hasattr(wf_cls, "__temporal_workflow_definition")

    def test_generated_class_registered_in_source_module(self) -> None:
        """Generated class is injected into the app's source module namespace."""
        import sys

        from application_sdk.app.base import generate_workflow_class

        class ModuleInjectedApp(App):
            async def run(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        meta = AppRegistry.get_instance().get("module-injected-app")
        ep = meta.entry_points["run"]
        wf_cls = generate_workflow_class(ModuleInjectedApp, ep)
        src_module = sys.modules.get(ModuleInjectedApp.__module__)
        assert src_module is not None
        assert getattr(src_module, wf_cls.__name__, None) is wf_cls

    def test_different_entry_points_produce_different_classes(self) -> None:
        """Each entry point on a multi-EP app gets its own distinct class."""
        from application_sdk.app.base import generate_workflow_class
        from application_sdk.app.entrypoint import entrypoint

        class TwoEpApp(App):
            @entrypoint
            async def step_one(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

            @entrypoint
            async def step_two(self, input: _WfInput2) -> _WfOutput2:
                return _WfOutput2()

        meta = AppRegistry.get_instance().get("two-ep-app")
        ep1 = meta.entry_points["step-one"]
        ep2 = meta.entry_points["step-two"]
        cls1 = generate_workflow_class(TwoEpApp, ep1)
        cls2 = generate_workflow_class(TwoEpApp, ep2)
        assert cls1 is not cls2
        defn1 = getattr(cls1, "__temporal_workflow_definition")
        defn2 = getattr(cls2, "__temporal_workflow_definition")
        assert defn1.name == "two-ep-app:step-one"
        assert defn2.name == "two-ep-app:step-two"

    def test_entrypoint_alias_registers_extra_bare_workflow(self) -> None:
        """An entry point's aliases each emit an extra workflow class registered
        under the bare alias name, without becoming separate entry points."""
        from application_sdk.app.entrypoint import entrypoint

        class AliasApp(App):
            name = "alias-app"

            @entrypoint(name="crawler", aliases=["alias-app"])
            async def crawler(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

            @entrypoint
            async def miner(self, input: _WfInput2) -> _WfOutput2:
                return _WfOutput2()

        meta = AppRegistry.get_instance().get("alias-app")
        # The alias must NOT leak into entry_points (no form/manifest/default impact).
        assert set(meta.entry_points) == {"crawler", "miner"}

        names = {
            getattr(wf, "__temporal_workflow_definition").name
            for wf in get_all_app_workflows()
        }
        # Canonical namespaced names plus the bare alias.
        assert {"alias-app", "alias-app:crawler", "alias-app:miner"} <= names

    def test_entrypoint_alias_delegates_to_same_method(self) -> None:
        """The alias workflow class shares the aliased entry point's run body:
        it is generated from the same ep, only the registered name differs."""
        from application_sdk.app.base import generate_workflow_class
        from application_sdk.app.entrypoint import entrypoint

        class AliasMethodApp(App):
            name = "alias-method-app"

            @entrypoint(name="crawler", aliases=["alias-method-app"])
            async def crawler(self, input: _WfInput) -> _WfOutput:
                return _WfOutput()

        meta = AppRegistry.get_instance().get("alias-method-app")
        ep = meta.entry_points["crawler"]
        canonical = generate_workflow_class(AliasMethodApp, ep)
        alias = generate_workflow_class(
            AliasMethodApp, ep, workflow_name_override="alias-method-app"
        )
        assert canonical is not alias
        assert (
            getattr(canonical, "__temporal_workflow_definition").name
            == "alias-method-app:crawler"
        )
        assert (
            getattr(alias, "__temporal_workflow_definition").name == "alias-method-app"
        )

    def test_invalid_entrypoint_alias_rejected(self) -> None:
        """A non-identifier alias is rejected at decoration time."""
        import pytest

        from application_sdk.app.entrypoint import (
            EntryPointContractError,
            entrypoint,
        )

        with pytest.raises(EntryPointContractError):

            class BadAliasApp(App):
                name = "bad-alias-app"

                @entrypoint(name="crawler", aliases=["not a valid name"])
                async def crawler(self, input: _WfInput) -> _WfOutput:
                    return _WfOutput()

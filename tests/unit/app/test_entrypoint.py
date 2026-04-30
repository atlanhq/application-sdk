"""Unit tests for @entrypoint decorator and EntryPointMetadata."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.entrypoint import (
    EntryPointContractError,
    EntryPointMetadata,
    entrypoint,
    get_entrypoint_metadata,
    is_entrypoint,
)
from application_sdk.app.registry import AppRegistry
from application_sdk.contracts.base import Input, Output

# ---------------------------------------------------------------------------
# Shared contract types
# ---------------------------------------------------------------------------


@dataclass
class _EpInput(Input):
    value: str = ""


@dataclass
class _EpOutput(Output):
    result: str = ""


@dataclass
class _EpInput2(Input):
    name: str = ""


@dataclass
class _EpOutput2(Output):
    count: int = 0


# ---------------------------------------------------------------------------
# Tests: decorator mechanics
# ---------------------------------------------------------------------------


class TestEntrypointDecorator:
    """Tests for the @entrypoint decorator itself."""

    def test_attaches_entrypoint_metadata(self) -> None:
        """@entrypoint sets _entrypoint_metadata on the function."""

        @entrypoint
        async def my_method(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        assert hasattr(my_method, "_entrypoint_metadata")

    def test_metadata_fields_are_correct(self) -> None:
        """EntryPointMetadata fields are populated from the method signature."""

        @entrypoint
        async def extract_data(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        meta: EntryPointMetadata = extract_data._entrypoint_metadata  # type: ignore[attr-defined]
        assert meta.name == "extract-data"
        assert meta.input_type is _EpInput
        assert meta.output_type is _EpOutput
        assert meta.method_name == "extract_data"
        assert meta.implicit is False

    def test_custom_name_override(self) -> None:
        """@entrypoint(name=...) overrides the kebab-case derived name."""

        @entrypoint(name="custom-name")
        async def some_method(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        meta: EntryPointMetadata = some_method._entrypoint_metadata  # type: ignore[attr-defined]
        assert meta.name == "custom-name"
        assert meta.method_name == "some_method"

    def test_is_entrypoint_returns_true(self) -> None:
        """is_entrypoint() returns True for decorated functions."""

        @entrypoint
        async def ep(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        assert is_entrypoint(ep) is True

    def test_is_entrypoint_returns_false_for_plain_method(self) -> None:
        """is_entrypoint() returns False for non-decorated functions."""

        async def plain(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        assert is_entrypoint(plain) is False

    def test_get_entrypoint_metadata_returns_metadata(self) -> None:
        """get_entrypoint_metadata() returns the attached metadata."""

        @entrypoint
        async def ep2(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        meta = get_entrypoint_metadata(ep2)
        assert meta is not None
        assert isinstance(meta, EntryPointMetadata)

    def test_get_entrypoint_metadata_returns_none_for_plain(self) -> None:
        """get_entrypoint_metadata() returns None for non-decorated functions."""

        async def plain(self: object, input: _EpInput) -> _EpOutput:
            return _EpOutput()

        assert get_entrypoint_metadata(plain) is None


# ---------------------------------------------------------------------------
# Tests: contract validation errors
# ---------------------------------------------------------------------------


class TestEntrypointContractErrors:
    """Tests for EntryPointContractError raised on invalid signatures."""

    def test_no_params_raises_contract_error(self) -> None:
        """Method with no params (besides self) raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError):

            @entrypoint
            async def bad(self: object) -> _EpOutput:  # type: ignore[misc]
                return _EpOutput()

    def test_two_params_raises_contract_error(self) -> None:
        """Method with two params raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError):

            @entrypoint
            async def bad2(  # type: ignore[misc]
                self: object, a: _EpInput, b: _EpInput
            ) -> _EpOutput:
                return _EpOutput()

    def test_wrong_input_type_raises_contract_error(self) -> None:
        """Parameter not extending Input raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError):

            @entrypoint
            async def bad3(self: object, input: str) -> _EpOutput:  # type: ignore[misc]
                return _EpOutput()

    def test_wrong_return_type_raises_contract_error(self) -> None:
        """Return type not extending Output raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError):

            @entrypoint
            async def bad4(self: object, input: _EpInput) -> str:  # type: ignore[misc]
                return "bad"


# ---------------------------------------------------------------------------
# Tests: App auto-registration with @entrypoint
# ---------------------------------------------------------------------------


class TestEntrypointAppRegistration:
    """Tests for App subclass auto-registration with @entrypoint methods."""

    @pytest.fixture(autouse=True)
    def _reset_registries(self, clean_app_registry, clean_task_registry) -> None:  # type: ignore[no-untyped-def]
        pass

    def test_app_with_entrypoint_auto_registers(self) -> None:
        """An App with @entrypoint methods registers in the AppRegistry."""

        class SingleEpApp(App):
            @entrypoint
            async def process(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        registry = AppRegistry.get_instance()
        meta = registry.get("single-ep-app")
        assert meta is not None
        assert meta.name == "single-ep-app"

    def test_app_with_entrypoint_has_entry_points_in_metadata(self) -> None:
        """AppMetadata.entry_points contains the registered entry points."""

        class EpMetaApp(App):
            @entrypoint
            async def ingest(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        registry = AppRegistry.get_instance()
        meta = registry.get("ep-meta-app")
        assert "ingest" in meta.entry_points
        ep = meta.entry_points["ingest"]
        assert ep.input_type is _EpInput
        assert ep.output_type is _EpOutput
        assert ep.method_name == "ingest"
        assert ep.implicit is False

    def test_multi_entrypoint_app_has_all_entry_points(self) -> None:
        """AppMetadata.entry_points contains all @entrypoint methods."""

        class MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

            @entrypoint
            async def mine(self, input: _EpInput2) -> _EpOutput2:
                return _EpOutput2()

        registry = AppRegistry.get_instance()
        meta = registry.get("multi-ep-app")
        assert "extract" in meta.entry_points
        assert "mine" in meta.entry_points

    def test_run_based_app_registers_with_implicit_entry_point(self) -> None:
        """A run()-based App registers with an implicit entry point."""

        class RunBasedApp(App):
            async def run(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        registry = AppRegistry.get_instance()
        meta = registry.get("run-based-app")
        assert "run" in meta.entry_points
        ep = meta.entry_points["run"]
        assert ep.implicit is True
        assert ep.method_name == "run"
        assert ep.input_type is _EpInput
        assert ep.output_type is _EpOutput

    def test_implicit_entry_point_workflow_name_has_no_colon(self) -> None:
        """Implicit (run-based) single entry point generates workflow name without colon."""
        from application_sdk.app.base import generate_workflow_class

        class ImplicitWfApp(App):
            async def run(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        registry = AppRegistry.get_instance()
        meta = registry.get("implicit-wf-app")
        ep = meta.entry_points["run"]
        wf_cls = generate_workflow_class(ImplicitWfApp, ep)
        defn = getattr(wf_cls, "__temporal_workflow_definition")
        assert defn.name == "implicit-wf-app"

    def test_explicit_entrypoint_workflow_name_uses_colon(self) -> None:
        """Explicit @entrypoint generates workflow name with colon separator."""
        from application_sdk.app.base import generate_workflow_class

        class ExplicitWfApp(App):
            @entrypoint
            async def do_work(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        registry = AppRegistry.get_instance()
        meta = registry.get("explicit-wf-app")
        ep = meta.entry_points["do-work"]
        wf_cls = generate_workflow_class(ExplicitWfApp, ep)
        defn = getattr(wf_cls, "__temporal_workflow_definition")
        assert defn.name == "explicit-wf-app:do-work"

    def test_multi_entrypoint_workflow_names_use_colon(self) -> None:
        """Multi-entry-point App generates 'app-name:ep-name' workflow names."""
        from application_sdk.app.base import generate_workflow_class

        class ColonWfApp(App):
            @entrypoint
            async def extract_metadata(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

            @entrypoint
            async def mine_queries(self, input: _EpInput2) -> _EpOutput2:
                return _EpOutput2()

        registry = AppRegistry.get_instance()
        meta = registry.get("colon-wf-app")

        ep_extract = meta.entry_points["extract-metadata"]
        wf_extract = generate_workflow_class(ColonWfApp, ep_extract)
        defn_extract = getattr(wf_extract, "__temporal_workflow_definition")
        assert defn_extract.name == "colon-wf-app:extract-metadata"

        ep_mine = meta.entry_points["mine-queries"]
        wf_mine = generate_workflow_class(ColonWfApp, ep_mine)
        defn_mine = getattr(wf_mine, "__temporal_workflow_definition")
        assert defn_mine.name == "colon-wf-app:mine-queries"

    def test_entrypoint_with_non_input_type_raises_contract_error(self) -> None:
        """Defining an App whose @entrypoint has a non-Input input_type raises EntryPointContractError.

        The @entrypoint decorator validates at decoration time, but __init_subclass__
        re-validates as defense-in-depth (e.g. manually crafted _entrypoint_metadata).
        Unlike the implicit run() path that silently skips template base classes,
        explicit @entrypoint decoration is always intentional — so we raise loudly.
        """
        import types

        from application_sdk.app.entrypoint import EntryPointContractError

        async def bad_ep(self: object, input: object) -> _EpOutput:
            return _EpOutput()

        # Manually attach metadata with an invalid input type, bypassing the
        # decorator's own validation so __init_subclass__ must catch it.
        bad_ep._entrypoint_metadata = EntryPointMetadata(  # type: ignore[attr-defined]
            name="bad-ep",
            input_type=str,  # str does not extend Input
            output_type=_EpOutput,
            method_name="bad_ep",
        )

        with pytest.raises(EntryPointContractError, match="input type"):
            types.new_class(
                "BadInputEntrypointApp",
                (App,),
                {},
                lambda ns: ns.update({"process": bad_ep}),
            )

    def test_entrypoint_with_non_output_type_raises_contract_error(self) -> None:
        """Defining an App whose @entrypoint has a non-Output output_type raises EntryPointContractError."""
        import types

        from application_sdk.app.entrypoint import EntryPointContractError

        async def bad_out_ep(self: object, input: _EpInput) -> object:
            return object()

        bad_out_ep._entrypoint_metadata = EntryPointMetadata(  # type: ignore[attr-defined]
            name="bad-out-ep",
            input_type=_EpInput,
            output_type=str,  # str does not extend Output
            method_name="bad_out_ep",
        )

        with pytest.raises(EntryPointContractError, match="output type"):
            types.new_class(
                "BadOutputEntrypointApp",
                (App,),
                {},
                lambda ns: ns.update({"process": bad_out_ep}),
            )

    def test_entrypoint_missing_return_annotation_raises_contract_error(self) -> None:
        """@entrypoint without a return type annotation raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError, match="return type"):

            @entrypoint
            async def no_return(self: object, input: _EpInput):  # type: ignore[return]
                return _EpOutput()

    def test_entrypoint_custom_name_invalid_identifier_raises_contract_error(
        self,
    ) -> None:
        """@entrypoint(name=...) with an invalid identifier raises EntryPointContractError."""
        with pytest.raises(EntryPointContractError, match="valid identifier"):

            @entrypoint(name="bad name with spaces!")
            async def bad_named(self: object, input: _EpInput) -> _EpOutput:  # type: ignore[misc]
                return _EpOutput()

    def test_entry_points_on_metadata_are_immutable(self) -> None:
        """AppMetadata.entry_points is a read-only MappingProxyType."""
        import types

        class ImmutableEpApp(App):
            @entrypoint
            async def work(self, input: _EpInput) -> _EpOutput:
                return _EpOutput()

        meta = AppRegistry.get_instance().get("immutable-ep-app")
        assert isinstance(meta.entry_points, types.MappingProxyType)
        with pytest.raises(TypeError):
            meta.entry_points["injected"] = meta.entry_points["work"]  # type: ignore[index]

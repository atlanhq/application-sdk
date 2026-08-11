"""Unit tests for the per-entry-point Temporal workflow type override (CNCT-199).

An app carrying an established bare workflow type (e.g. ``KeifuWorkflow``)
cannot reproduce it through the ``{app-name}:{entry-point-name}`` convention.
``@entrypoint(workflow_type=...)`` moves Temporal registration only: the
override becomes the primary type, the canonical name stays registered as an
alias, and both resolve back to the same entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.entrypoint import (
    EntryPointContractError,
    EntryPointMetadata,
    build_workflow_type_index,
    canonical_workflow_type,
    entrypoint,
    get_entrypoint_metadata,
    primary_workflow_type,
    workflow_type_class_segment,
    workflow_types_for,
)
from application_sdk.app.registry import AppMetadata, AppRegistry
from application_sdk.contracts.base import Input, Output

# ---------------------------------------------------------------------------
# Shared contract types
# ---------------------------------------------------------------------------


@dataclass
class _QiInput(Input):
    value: str = ""


@dataclass
class _QiOutput(Output):
    result: str = ""


@dataclass
class _KeifuInput(Input):
    partition: str = ""


@dataclass
class _KeifuOutput(Output):
    count: int = 0


def _ep(
    name: str,
    *,
    implicit: bool = False,
    workflow_type: str | None = None,
    output_type: type[Output] = _QiOutput,
) -> EntryPointMetadata:
    """Build EntryPointMetadata directly, without going through a class."""
    return EntryPointMetadata(
        name=name,
        input_type=_QiInput,
        output_type=output_type,
        method_name=name.replace("-", "_"),
        implicit=implicit,
        workflow_type=workflow_type,
    )


# ---------------------------------------------------------------------------
# Decorator surface
# ---------------------------------------------------------------------------


class TestDecoratorAcceptsOverride:
    def test_stores_override_on_metadata(self) -> None:
        @entrypoint(name="keifu", workflow_type="KeifuWorkflow")
        async def keifu(self: object, input: _KeifuInput) -> _KeifuOutput:
            return _KeifuOutput()

        meta = get_entrypoint_metadata(keifu)
        assert meta is not None
        assert meta.name == "keifu"
        assert meta.workflow_type == "KeifuWorkflow"

    def test_defaults_to_none(self) -> None:
        @entrypoint
        async def mine_queries(self: object, input: _QiInput) -> _QiOutput:
            return _QiOutput()

        meta = get_entrypoint_metadata(mine_queries)
        assert meta is not None
        assert meta.workflow_type is None

    def test_colon_is_legal(self) -> None:
        """A colon is a valid Temporal type — 'teradata-app:crawler' is a real shape."""

        @entrypoint(name="crawler", workflow_type="teradata-app:crawler")
        async def crawler(self: object, input: _QiInput) -> _QiOutput:
            return _QiOutput()

        meta = get_entrypoint_metadata(crawler)
        assert meta is not None
        assert meta.workflow_type == "teradata-app:crawler"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "Keifu Workflow",
            "Keifu\tWorkflow",
            "Keifu\nWorkflow",
            ":",
            "::-_",
            "Keifu\x00Workflow",
        ],
    )
    def test_rejects_unusable_override(self, bad: str) -> None:
        with pytest.raises(EntryPointContractError):

            @entrypoint(name="keifu", workflow_type=bad)
            async def keifu(self: object, input: _QiInput) -> _QiOutput:
                return _QiOutput()

    @pytest.mark.parametrize(
        "legacy_type",
        [
            "9to5Workflow",
            "com.acme.MyWorkflow",
            "io.temporal.SampleWorkflow",
            "teradata-app:crawler",
        ],
        ids=["leading-digit", "java-fqn", "go-fqn", "colon"],
    )
    def test_accepts_the_shapes_a_migrating_app_must_preserve(
        self, legacy_type: str
    ) -> None:
        """Temporal puts no charset limit on a workflow type.

        Rejecting a shape Temporal accepts would leave an app migrating off a
        Java or Go worker no way to keep its established type — the exact case
        this feature exists for.
        """

        @entrypoint(name="legacy", workflow_type=legacy_type)
        async def legacy(self: object, input: _QiInput) -> _QiOutput:
            return _QiOutput()

        meta = get_entrypoint_metadata(legacy)
        assert meta is not None
        assert meta.workflow_type == legacy_type

    def test_rejects_non_string(self) -> None:
        with pytest.raises(EntryPointContractError):

            @entrypoint(name="keifu", workflow_type=123)  # type: ignore[arg-type]
            async def keifu(self: object, input: _QiInput) -> _QiOutput:
                return _QiOutput()


# ---------------------------------------------------------------------------
# Name derivation
# ---------------------------------------------------------------------------


class TestClassSegment:
    @pytest.mark.parametrize(
        ("workflow_type", "expected"),
        [
            ("KeifuWorkflow", "KeifuWorkflow"),
            ("query-intelligence:keifu", "query_intelligence_keifu"),
            ("com.acme.MyWorkflow", "com_acme_MyWorkflow"),
            ("9to5Workflow", "9to5Workflow"),
        ],
    )
    def test_folds_to_a_usable_class_name(
        self, workflow_type: str, expected: str
    ) -> None:
        segment = workflow_type_class_segment(workflow_type)
        assert segment == expected
        assert f"_Workflow_{segment}".isidentifier()


class TestWorkflowTypesFor:
    def test_canonical_without_override(self) -> None:
        ep = _ep("keifu")
        assert canonical_workflow_type("query-intelligence", ep) == (
            "query-intelligence:keifu"
        )
        assert workflow_types_for("query-intelligence", ep) == (
            "query-intelligence:keifu",
        )

    def test_implicit_entry_point_is_bare(self) -> None:
        ep = _ep("run", implicit=True)
        assert workflow_types_for("publish-app", ep) == ("publish-app",)

    def test_override_is_primary_canonical_is_alias(self) -> None:
        ep = _ep("keifu", workflow_type="KeifuWorkflow")
        assert workflow_types_for("query-intelligence", ep) == (
            "KeifuWorkflow",
            "query-intelligence:keifu",
        )
        assert primary_workflow_type("query-intelligence", ep) == "KeifuWorkflow"

    def test_override_equal_to_canonical_registers_once(self) -> None:
        """A redundant override must not register the same name twice."""
        ep = _ep("keifu", workflow_type="query-intelligence:keifu")
        assert workflow_types_for("query-intelligence", ep) == (
            "query-intelligence:keifu",
        )

    def test_primary_without_override_is_canonical(self) -> None:
        ep = _ep("keifu")
        assert primary_workflow_type("query-intelligence", ep) == (
            "query-intelligence:keifu"
        )


# ---------------------------------------------------------------------------
# Index construction and collision rules
# ---------------------------------------------------------------------------


class TestBuildWorkflowTypeIndex:
    def test_indexes_override_and_alias(self) -> None:
        eps = {
            "query-intelligence": _ep(
                "query-intelligence", workflow_type="QueryIntelligenceWorkflow"
            ),
            "keifu": _ep(
                "keifu", workflow_type="KeifuWorkflow", output_type=_KeifuOutput
            ),
        }
        index = build_workflow_type_index("query-intelligence", eps)

        assert set(index) == {
            "QueryIntelligenceWorkflow",
            "query-intelligence:query-intelligence",
            "KeifuWorkflow",
            "query-intelligence:keifu",
        }
        assert index["KeifuWorkflow"] is eps["keifu"]
        assert index["query-intelligence:keifu"] is eps["keifu"]

    def test_no_overrides_indexes_canonical_only(self) -> None:
        eps = {"keifu": _ep("keifu"), "mine": _ep("mine")}
        index = build_workflow_type_index("qi", eps)
        assert set(index) == {"qi:keifu", "qi:mine"}

    def test_rejects_two_entry_points_claiming_same_override(self) -> None:
        eps = {
            "keifu": _ep("keifu", workflow_type="SharedWorkflow"),
            "mine": _ep("mine", workflow_type="SharedWorkflow"),
        }
        with pytest.raises(EntryPointContractError, match="SharedWorkflow"):
            build_workflow_type_index("qi", eps)

    def test_rejects_override_colliding_with_another_canonical(self) -> None:
        eps = {
            "keifu": _ep("keifu", workflow_type="qi:mine"),
            "mine": _ep("mine"),
        }
        with pytest.raises(EntryPointContractError, match="qi:mine"):
            build_workflow_type_index("qi", eps)

    def test_rejects_override_colliding_with_implicit_bare_name(self) -> None:
        eps = {
            "run": _ep("run", implicit=True),
            "keifu": _ep("keifu", workflow_type="qi"),
        }
        with pytest.raises(EntryPointContractError, match="'qi'"):
            build_workflow_type_index("qi", eps)

    def test_rejects_types_that_fold_to_one_class_name(self) -> None:
        """'qi:bar' and 'qi-bar' are distinct types but one generated class.

        Both hyphen and colon become '_', so the second generated class would
        overwrite the first in the module namespace and Temporal's sandbox would
        re-import the survivor for both types — silently running the wrong
        entry point.
        """
        eps = {
            "bar": _ep("bar"),
            "baz": _ep("baz", workflow_type="qi-bar"),
        }
        with pytest.raises(EntryPointContractError, match="_Workflow_qi_bar"):
            build_workflow_type_index("qi", eps)

    def test_distinct_class_segments_are_fine(self) -> None:
        eps = {
            "bar": _ep("bar"),
            "baz": _ep("baz", workflow_type="QiBar"),
        }
        index = build_workflow_type_index("qi", eps)
        assert "QiBar" in index and "qi:bar" in index


# ---------------------------------------------------------------------------
# AppMetadata carries the index
# ---------------------------------------------------------------------------


class TestAppMetadataIndex:
    def _meta(self) -> AppMetadata:
        return AppMetadata(
            name="query-intelligence",
            version="1.0.0",
            app_cls=object,
            input_type=_QiInput,
            output_type=_QiOutput,
            entry_points={
                "keifu": _ep(
                    "keifu", workflow_type="KeifuWorkflow", output_type=_KeifuOutput
                ),
            },
        )

    def test_index_is_derived_at_construction(self) -> None:
        meta = self._meta()
        assert set(meta.workflow_types) == {
            "KeifuWorkflow",
            "query-intelligence:keifu",
        }
        assert meta.workflow_types["KeifuWorkflow"].output_type is _KeifuOutput

    def test_index_is_frozen(self) -> None:
        meta = self._meta()
        with pytest.raises(TypeError):
            meta.workflow_types["Injected"] = _ep("injected")  # type: ignore[index]

    def test_empty_entry_points_gives_empty_index(self) -> None:
        meta = AppMetadata(
            name="qi",
            version="1.0.0",
            app_cls=object,
            input_type=_QiInput,
            output_type=_QiOutput,
        )
        assert dict(meta.workflow_types) == {}


# ---------------------------------------------------------------------------
# End-to-end through App registration
# ---------------------------------------------------------------------------


class TestAppRegistration:
    def test_registered_app_exposes_both_names(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        class QueryIntelligenceApp(App):
            name = "query-intelligence"
            version = "1.0.0"

            @entrypoint(default=True, workflow_type="QueryIntelligenceWorkflow")
            async def query_intelligence(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

            @entrypoint(name="keifu", workflow_type="KeifuWorkflow")
            async def keifu(
                self, input: _KeifuInput
            ) -> _KeifuOutput:  # pragma: no cover - not executed
                return _KeifuOutput()

        meta = AppRegistry.get_instance().get("query-intelligence")
        assert set(meta.workflow_types) == {
            "QueryIntelligenceWorkflow",
            "query-intelligence:query-intelligence",
            "KeifuWorkflow",
            "query-intelligence:keifu",
        }
        assert meta.workflow_types["KeifuWorkflow"].name == "keifu"

    def test_collision_fails_at_class_definition(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        with pytest.raises(EntryPointContractError):

            class CollidingApp(App):
                name = "colliding"
                version = "1.0.0"

                @entrypoint(name="a", workflow_type="SameWorkflow")
                async def a(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

                @entrypoint(name="b", workflow_type="SameWorkflow")
                async def b(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    def test_worker_registers_one_class_per_type(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        from application_sdk.execution._temporal.workflows import get_all_app_workflows

        class AliasedApp(App):
            name = "aliased"
            version = "1.0.0"

            @entrypoint(default=True, workflow_type="AliasedWorkflow")
            async def extract(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        registered = {
            getattr(wf_cls, "__temporal_workflow_definition").name
            for wf_cls in get_all_app_workflows()
        }
        assert {"AliasedWorkflow", "aliased:extract"} <= registered


# ---------------------------------------------------------------------------
# Handler: reverse resolution and inbound selection
# ---------------------------------------------------------------------------


@pytest.fixture
def _qi_app(clean_app_registry: object, clean_task_registry: object):  # type: ignore[no-untyped-def]
    """Register a two-entry-point app with overrides and point the handler at it."""
    from application_sdk.handler import service as svc

    class QueryIntelligenceApp(App):
        name = "query-intelligence"
        version = "1.0.0"

        @entrypoint(default=True, workflow_type="QueryIntelligenceWorkflow")
        async def query_intelligence(
            self, input: _QiInput
        ) -> _QiOutput:  # pragma: no cover - not executed
            return _QiOutput()

        @entrypoint(name="keifu", workflow_type="KeifuWorkflow")
        async def keifu(
            self, input: _KeifuInput
        ) -> _KeifuOutput:  # pragma: no cover - not executed
            return _KeifuOutput()

    previous = svc._workflow_config
    svc._workflow_config = svc.WorkflowClientConfig(
        app_name="query-intelligence",
        app_class=QueryIntelligenceApp,
    )
    yield QueryIntelligenceApp
    svc._workflow_config = previous


class TestResolveOutputType:
    def test_resolves_override(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_output_type_for_workflow

        assert _resolve_output_type_for_workflow("KeifuWorkflow") is _KeifuOutput

    def test_resolves_canonical_alias(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_output_type_for_workflow

        assert (
            _resolve_output_type_for_workflow("query-intelligence:keifu")
            is _KeifuOutput
        )

    @pytest.mark.parametrize(
        "unknown",
        ["NotRegisteredWorkflow", "other-app:keifu", "query-intelligence:missing"],
    )
    def test_unknown_type_returns_none(self, _qi_app: type, unknown: str) -> None:
        from application_sdk.handler.service import _resolve_output_type_for_workflow

        assert _resolve_output_type_for_workflow(unknown) is None


class TestInboundSelector:
    def test_entry_point_name_still_wins(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint("query-intelligence", "keifu")
        assert ep.name == "keifu"

    def test_falls_back_to_registered_workflow_type(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint("query-intelligence", "KeifuWorkflow")
        assert ep.name == "keifu"

    def test_canonical_type_also_selects(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint(
            "query-intelligence", "query-intelligence:keifu"
        )
        assert ep.name == "keifu"

    def test_unknown_selector_still_rejected(self, _qi_app: type) -> None:
        from fastapi import HTTPException

        from application_sdk.handler.service import _resolve_app_entrypoint

        with pytest.raises(HTTPException) as excinfo:
            _resolve_app_entrypoint("query-intelligence", "NoSuchThing")
        assert excinfo.value.status_code == 400

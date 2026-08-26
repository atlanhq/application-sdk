"""Unit tests for inbound-only legacy workflow type aliases (CNCT-199).

An app carrying an established bare workflow type (e.g. ``KeifuWorkflow``)
cannot reproduce it through the ``{app-name}:{entry-point-name}`` convention.
``App.legacy_workflow_types`` declares those types as inbound-only aliases:
the worker registers them so a caller already dispatching one still reaches
the entry point, while every SDK-initiated dispatch emits the canonical
convention-derived type. The alias is accepted, never produced.
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
    workflow_type_class_segment,
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


_UNSET = object()


def _ep(
    name: str,
    *,
    implicit: bool = False,
    output_type: type[Output] = _QiOutput,
) -> EntryPointMetadata:
    """Build EntryPointMetadata directly, without going through a class."""
    return EntryPointMetadata(
        name=name,
        input_type=_QiInput,
        output_type=output_type,
        method_name=name.replace("-", "_"),
        implicit=implicit,
    )


# ---------------------------------------------------------------------------
# Canonical naming
# ---------------------------------------------------------------------------


class TestCanonicalWorkflowType:
    def test_explicit_entry_point_is_prefixed(self) -> None:
        assert canonical_workflow_type("qi", _ep("keifu")) == "qi:keifu"

    def test_implicit_entry_point_is_bare_app_name(self) -> None:
        assert canonical_workflow_type("qi", _ep("run", implicit=True)) == "qi"


# ---------------------------------------------------------------------------
# The index: canonical types plus declared aliases
# ---------------------------------------------------------------------------


class TestBuildWorkflowTypeIndex:
    def test_alias_and_canonical_both_resolve_to_the_entry_point(self) -> None:
        """A1: a declared alias registers alongside the canonical type."""
        keifu = _ep("keifu", output_type=_KeifuOutput)
        index = build_workflow_type_index(
            "query-intelligence",
            {"keifu": keifu},
            {"KeifuWorkflow": "keifu"},
        )
        assert index["KeifuWorkflow"] is keifu
        assert index["query-intelligence:keifu"] is keifu
        assert set(index) == {"KeifuWorkflow", "query-intelligence:keifu"}

    @pytest.mark.parametrize("legacy", [None, {}])
    def test_no_aliases_indexes_canonical_only(self, legacy: object) -> None:
        """A8: absent or empty declaration keeps today's behavior exactly."""
        index = build_workflow_type_index(
            "qi",
            {"keifu": _ep("keifu")},
            legacy,  # type: ignore[arg-type]
        )
        assert set(index) == {"qi:keifu"}

    def test_rejects_alias_equal_to_own_canonical_type(self) -> None:
        """A2: restating the canonical name is a mistake, not an alias."""
        with pytest.raises(EntryPointContractError, match="canonical"):
            build_workflow_type_index(
                "qi", {"keifu": _ep("keifu")}, {"qi:keifu": "keifu"}
            )

    def test_rejects_alias_equal_to_sibling_canonical_type(self) -> None:
        """A2: an alias may not shadow another entry point's canonical type."""
        with pytest.raises(EntryPointContractError, match="canonical"):
            build_workflow_type_index(
                "qi",
                {"keifu": _ep("keifu"), "miner": _ep("miner")},
                {"qi:miner": "keifu"},
            )

    def test_rejects_alias_equal_to_implicit_bare_app_name(self) -> None:
        """A2: the bare app name is the implicit entry point's canonical type."""
        with pytest.raises(EntryPointContractError, match="canonical"):
            build_workflow_type_index(
                "qi",
                {"run": _ep("run", implicit=True), "keifu": _ep("keifu")},
                {"qi": "keifu"},
            )

    def test_rejects_alias_targeting_unknown_entry_point(self) -> None:
        """A4: the target must name a registered entry point."""
        with pytest.raises(EntryPointContractError, match="keifu"):
            build_workflow_type_index(
                "qi", {"keifu": _ep("keifu")}, {"KeifuWorkflow": "kiefu"}
            )

    def test_rejects_alias_equal_to_an_entry_point_name(self) -> None:
        """A11: names and types stay disjoint so selectors are never ambiguous."""
        with pytest.raises(EntryPointContractError, match="entry point name"):
            build_workflow_type_index(
                "qi",
                {"keifu": _ep("keifu"), "miner": _ep("miner")},
                {"miner": "keifu"},
            )

    def test_rejects_entry_point_name_equal_to_bare_canonical_type(self) -> None:
        """A12: with an implicit run(), the bare app name is a canonical type,
        so an explicit entry point named exactly like the app would make one
        selector string mean two entry points. Rejected at registration."""
        with pytest.raises(EntryPointContractError, match="canonical"):
            build_workflow_type_index(
                "postgres",
                {"run": _ep("run", implicit=True), "postgres": _ep("postgres")},
                None,
            )

    @pytest.mark.parametrize(
        "bad",
        ["", "Keifu Workflow", "Keifu\tWorkflow", "Keifu\x00Workflow", ":::", "___"],
    )
    def test_rejects_unusable_alias_strings(self, bad: str) -> None:
        """A5: whitespace, control characters, or no identifying content."""
        with pytest.raises(EntryPointContractError):
            build_workflow_type_index("qi", {"keifu": _ep("keifu")}, {bad: "keifu"})

    @pytest.mark.parametrize(
        "alias",
        [
            "KeifuWorkflow",
            "com.acme.MyWorkflow",
            "9to5Workflow",
            "teradata-app:crawler",
        ],
    )
    def test_accepts_the_shapes_a_migrating_app_must_preserve(self, alias: str) -> None:
        index = build_workflow_type_index(
            "qi", {"keifu": _ep("keifu")}, {alias: "keifu"}
        )
        assert index[alias].name == "keifu"

    def test_rejects_alias_folding_to_a_canonical_class_name(self) -> None:
        """A6: ``qi:keifu`` and ``qi_keifu`` share one generated class name."""
        with pytest.raises(EntryPointContractError, match="_Workflow_"):
            build_workflow_type_index(
                "qi", {"keifu": _ep("keifu")}, {"qi_keifu": "keifu"}
            )

    def test_rejects_two_aliases_folding_together(self) -> None:
        with pytest.raises(EntryPointContractError, match="_Workflow_"):
            build_workflow_type_index(
                "qi",
                {"keifu": _ep("keifu"), "miner": _ep("miner")},
                {"Legacy-Type": "keifu", "Legacy:Type": "miner"},
            )

    @pytest.mark.parametrize(
        "legacy",
        [
            {1: "keifu"},
            {"KeifuWorkflow": 1},
            {"KeifuWorkflow": None},
        ],
    )
    def test_rejects_non_string_entries(self, legacy: dict) -> None:
        """A9: keys and values must both be strings."""
        with pytest.raises(EntryPointContractError):
            build_workflow_type_index("qi", {"keifu": _ep("keifu")}, legacy)


class TestClassSegment:
    @pytest.mark.parametrize(
        ("workflow_type", "segment"),
        [
            ("query-intelligence:keifu", "query_intelligence_keifu"),
            ("com.acme.MyWorkflow", "com_acme_MyWorkflow"),
            ("KeifuWorkflow", "KeifuWorkflow"),
            ("9to5Workflow", "9to5Workflow"),
        ],
    )
    def test_folds_to_a_usable_class_name(
        self, workflow_type: str, segment: str
    ) -> None:
        assert workflow_type_class_segment(workflow_type) == segment
        assert f"_Workflow_{segment}".isidentifier()

    def test_folds_unicode_alphanumeric_that_is_not_identifier_safe(self) -> None:
        segment = workflow_type_class_segment("Keifu²Workflow")
        assert f"_Workflow_{segment}".isidentifier()


# ---------------------------------------------------------------------------
# AppMetadata derives the index from the class attribute
# ---------------------------------------------------------------------------


class TestAppMetadataIndex:
    def _meta(self, legacy: object = _UNSET) -> AppMetadata:
        kwargs: dict = {} if legacy is _UNSET else {"legacy_workflow_types": legacy}
        return AppMetadata(
            name="query-intelligence",
            version="1.0.0",
            app_cls=object,
            input_type=_QiInput,
            output_type=_QiOutput,
            entry_points={
                "keifu": _ep("keifu", output_type=_KeifuOutput),
            },
            **kwargs,
        )

    def test_index_includes_declared_aliases(self) -> None:
        meta = self._meta({"KeifuWorkflow": "keifu"})
        assert set(meta.workflow_types) == {
            "KeifuWorkflow",
            "query-intelligence:keifu",
        }
        assert meta.workflow_types["KeifuWorkflow"].output_type is _KeifuOutput

    def test_no_declaration_gives_canonical_only(self) -> None:
        meta = self._meta()
        assert set(meta.workflow_types) == {"query-intelligence:keifu"}

    def test_index_and_declaration_are_frozen(self) -> None:
        meta = self._meta({"KeifuWorkflow": "keifu"})
        with pytest.raises(TypeError):
            meta.workflow_types["Injected"] = _ep("injected")  # type: ignore[index]
        with pytest.raises(TypeError):
            meta.legacy_workflow_types["Injected"] = "keifu"  # type: ignore[index]

    @pytest.mark.parametrize("bad", ["KeifuWorkflow", ["KeifuWorkflow"], 0])
    def test_rejects_non_mapping_declaration(self, bad: object) -> None:
        """A9: a string, list, or number is a declaration mistake, not an
        empty map — including falsy shapes."""
        with pytest.raises(EntryPointContractError, match="legacy_workflow_types"):
            self._meta(bad)


# ---------------------------------------------------------------------------
# End-to-end through App registration
# ---------------------------------------------------------------------------


class TestAppRegistration:
    def test_registered_app_exposes_canonical_and_alias_types(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        class QueryIntelligenceApp(App):
            name = "query-intelligence"
            version = "1.0.0"
            legacy_workflow_types = {
                "QueryIntelligenceWorkflow": "query-intelligence",
                "KeifuWorkflow": "keifu",
            }

            @entrypoint(default=True)
            async def query_intelligence(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

            @entrypoint(name="keifu")
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

    def test_decorator_no_longer_accepts_workflow_type(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """The unreleased override parameter is gone, not deprecated."""
        with pytest.raises(TypeError):

            class LegacyParamApp(App):
                name = "legacy-param"

                @entrypoint(workflow_type="KeifuWorkflow")  # type: ignore[call-arg]
                async def keifu(
                    self, input: _KeifuInput
                ) -> _KeifuOutput:  # pragma: no cover - not executed
                    return _KeifuOutput()

    def test_bad_alias_fails_at_class_definition(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        with pytest.raises(EntryPointContractError, match="canonical"):

            class CollidingApp(App):
                name = "qi"
                legacy_workflow_types = {"qi:miner": "keifu"}

                @entrypoint(name="keifu")
                async def keifu(
                    self, input: _KeifuInput
                ) -> _KeifuOutput:  # pragma: no cover - not executed
                    return _KeifuOutput()

                @entrypoint(name="miner")
                async def miner(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    def test_worker_registers_one_class_per_type(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """B1/B2: canonical and alias classes registered once, idempotently."""
        from application_sdk.execution._temporal.workflows import get_all_app_workflows

        class AliasedApp(App):
            name = "aliased"
            legacy_workflow_types = {"LegacyAliased": "work"}

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        first = get_all_app_workflows()
        names = {cls.__name__ for cls in first}
        assert names == {
            "_Workflow_aliased_work",
            "_Workflow_LegacyAliased",
        }
        assert first == get_all_app_workflows()

    def test_subclass_does_not_inherit_parent_aliases(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """Aliases name one app's wire contract; the MRO must not propagate
        them into a subclass's registration."""

        class ParentAliasedApp(App):
            name = "parent-aliased"
            legacy_workflow_types = {"SharedLegacy": "work"}

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        class ChildApp(ParentAliasedApp):
            name = "child"

        child_meta = AppRegistry.get_instance().get("child")
        assert set(child_meta.workflow_types) == {"child:work"}
        assert dict(child_meta.legacy_workflow_types) == {}

    def test_falsy_non_mapping_declaration_fails_at_class_definition(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        with pytest.raises(EntryPointContractError, match="legacy_workflow_types"):

            class FalsyDeclarationApp(App):
                name = "falsy-decl"
                legacy_workflow_types = []  # type: ignore[assignment]

                @entrypoint
                async def work(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    def test_expired_removal_version_fails_at_class_definition(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """An opt-in expiry with teeth: once the SDK passes the declared
        removal version, keeping the aliases is a loud decision, not drift."""
        with pytest.raises(EntryPointContractError, match="removal"):

            class ExpiredAliasApp(App):
                name = "expired-alias"
                legacy_workflow_types = {"OldWorkflow": "work"}
                legacy_workflow_types_removal_version = "0.0.1"

                @entrypoint
                async def work(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    def test_future_removal_version_registers_normally(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        class UnexpiredAliasApp(App):
            name = "unexpired-alias"
            legacy_workflow_types = {"OldWorkflow": "work"}
            legacy_workflow_types_removal_version = "999.0.0"

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        meta = AppRegistry.get_instance().get("unexpired-alias")
        assert "OldWorkflow" in meta.workflow_types

    def test_prerelease_of_removal_version_is_not_expired(
        self,
        clean_app_registry: object,
        clean_task_registry: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A release candidate of the removal version is still pre-removal:
        4.2.0rc1 orders strictly below 4.2.0, so the aliases stay alive."""
        monkeypatch.setattr("application_sdk.version.__version__", "4.2.0rc1")

        class RcAliasApp(App):
            name = "rc-alias"
            legacy_workflow_types = {"OldWorkflow": "work"}
            legacy_workflow_types_removal_version = "4.2.0"

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        meta = AppRegistry.get_instance().get("rc-alias")
        assert "OldWorkflow" in meta.workflow_types

    def test_prerelease_past_removal_version_is_expired(
        self,
        clean_app_registry: object,
        clean_task_registry: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("application_sdk.version.__version__", "4.2.1rc1")
        with pytest.raises(EntryPointContractError, match="removal"):

            class PastRcAliasApp(App):
                name = "past-rc-alias"
                legacy_workflow_types = {"OldWorkflow": "work"}
                legacy_workflow_types_removal_version = "4.2.0"

                @entrypoint
                async def work(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    @pytest.mark.parametrize("installed", ["4.2.0a1", "4.2.0b2", "4.2.0.dev1"])
    def test_prerelease_segments_do_not_blame_the_removal_version(
        self,
        clean_app_registry: object,
        clean_task_registry: object,
        monkeypatch: pytest.MonkeyPatch,
        installed: str,
    ) -> None:
        """An alpha/beta/dev build of the removal version must register, not
        raise the error reserved for a malformed removal declaration."""
        monkeypatch.setattr("application_sdk.version.__version__", installed)

        class PreReleaseAliasApp(App):
            name = "prerelease-alias"
            legacy_workflow_types = {"OldWorkflow": "work"}
            legacy_workflow_types_removal_version = "4.2.0"

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        meta = AppRegistry.get_instance().get("prerelease-alias")
        assert "OldWorkflow" in meta.workflow_types

    def test_non_numeric_removal_version_is_rejected(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """The removal declaration itself stays dotted-numeric: a final
        release is the only expiry the contract accepts."""
        with pytest.raises(EntryPointContractError, match="dotted numeric"):

            class BadRemovalApp(App):
                name = "bad-removal"
                legacy_workflow_types = {"OldWorkflow": "work"}
                legacy_workflow_types_removal_version = "4.2.0rc1"

                @entrypoint
                async def work(
                    self, input: _QiInput
                ) -> _QiOutput:  # pragma: no cover - not executed
                    return _QiOutput()

    def test_removal_version_without_aliases_is_ignored(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        class NoAliasExpiryApp(App):
            name = "no-alias-expiry"
            legacy_workflow_types_removal_version = "0.0.1"

            @entrypoint
            async def work(
                self, input: _QiInput
            ) -> _QiOutput:  # pragma: no cover - not executed
                return _QiOutput()

        meta = AppRegistry.get_instance().get("no-alias-expiry")
        assert set(meta.workflow_types) == {"no-alias-expiry:work"}

    def test_postgres_like_mixed_app_keeps_all_existing_workflow_types(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """A run() + crawler + miner app must keep its established type set."""

        class PostgresLikeApp(App):
            name = "postgres"

            async def run(self, input: _QiInput) -> _QiOutput:
                return _QiOutput()

            @entrypoint
            async def crawler(self, input: _QiInput) -> _QiOutput:
                return _QiOutput()

            @entrypoint
            async def miner(self, input: _KeifuInput) -> _KeifuOutput:
                return _KeifuOutput()

        meta = AppRegistry.get_instance().get("postgres")
        assert set(meta.workflow_types) == {
            "postgres",
            "postgres:crawler",
            "postgres:miner",
        }
        assert meta.entry_points["run"].default is True


# ---------------------------------------------------------------------------
# Handler: reverse resolution and inbound selection
# ---------------------------------------------------------------------------


@pytest.fixture
def _qi_app(clean_app_registry: object, clean_task_registry: object):  # type: ignore[no-untyped-def]
    """Register a two-entry-point app with aliases and point the handler at it."""
    from application_sdk.handler import service as svc

    class QueryIntelligenceApp(App):
        name = "query-intelligence"
        version = "1.0.0"
        legacy_workflow_types = {
            "QueryIntelligenceWorkflow": "query-intelligence",
            "KeifuWorkflow": "keifu",
        }

        @entrypoint(default=True)
        async def query_intelligence(
            self, input: _QiInput
        ) -> _QiOutput:  # pragma: no cover - not executed
            return _QiOutput()

        @entrypoint(name="keifu")
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
    def test_resolves_alias(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_output_type_for_workflow

        assert _resolve_output_type_for_workflow("KeifuWorkflow") is _KeifuOutput

    def test_resolves_canonical(self, _qi_app: type) -> None:
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


class TestImplicitBareTypeSelector:
    def test_bare_app_name_in_body_selector_reaches_run(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        """A legacy body selector carrying the bare app name must keep
        reaching the implicit run() entry point on a mixed app."""
        from application_sdk.handler import service as svc
        from application_sdk.handler.service import _resolve_app_entrypoint

        class MixedPostgresApp(App):
            name = "postgres-mixed"

            async def run(self, input: _QiInput) -> _QiOutput:
                return _QiOutput()

            @entrypoint
            async def miner(
                self, input: _KeifuInput
            ) -> _KeifuOutput:  # pragma: no cover - not executed
                return _KeifuOutput()

        previous = svc._workflow_config
        svc._workflow_config = svc.WorkflowClientConfig(
            app_name="postgres-mixed",
            app_class=MixedPostgresApp,
        )
        try:
            _, ep = _resolve_app_entrypoint(
                "postgres-mixed", "postgres-mixed", allow_workflow_type=True
            )
            assert ep.implicit is True
        finally:
            svc._workflow_config = previous


class TestInboundSelector:
    def test_entry_point_name_resolves_on_every_surface(self, _qi_app: type) -> None:
        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint("query-intelligence", "keifu")
        assert ep.name == "keifu"

    def test_alias_resolves_only_when_workflow_types_are_allowed(
        self, _qi_app: type
    ) -> None:
        """D2: the deprecated body field accepts an alias; ``?entrypoint=``
        resolves entry-point names only, so an alias never becomes a second
        permanent name on the SDK's own HTTP surface."""
        from fastapi import HTTPException

        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint(
            "query-intelligence", "KeifuWorkflow", allow_workflow_type=True
        )
        assert ep.name == "keifu"

        with pytest.raises(HTTPException) as excinfo:
            _resolve_app_entrypoint("query-intelligence", "KeifuWorkflow")
        assert excinfo.value.status_code == 400

    def test_canonical_type_follows_the_same_rule(self, _qi_app: type) -> None:
        from fastapi import HTTPException

        from application_sdk.handler.service import _resolve_app_entrypoint

        _, ep = _resolve_app_entrypoint(
            "query-intelligence",
            "query-intelligence:keifu",
            allow_workflow_type=True,
        )
        assert ep.name == "keifu"

        with pytest.raises(HTTPException) as excinfo:
            _resolve_app_entrypoint("query-intelligence", "query-intelligence:keifu")
        assert excinfo.value.status_code == 400

    def test_unknown_selector_still_rejected(self, _qi_app: type) -> None:
        from fastapi import HTTPException

        from application_sdk.handler.service import _resolve_app_entrypoint

        with pytest.raises(HTTPException) as excinfo:
            _resolve_app_entrypoint(
                "query-intelligence", "NoSuchThing", allow_workflow_type=True
            )
        assert excinfo.value.status_code == 400

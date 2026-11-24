"""App and SqlApp configuration ClassVars are checked at class definition.

Each setting is read long after import and coerced silently by its reader, so
a typo there (``preflight_gate_mode = "hrad"``) used to mean soft with nothing
logged. Class definition now warns, naming the attribute, the value and the
v4.0.0 version that turns the warning into ``EntryPointDeclarationError``.

Every test defines its class *inside* the test body: the check runs from
``__init_subclass__``, so class definition is the act under test.
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar
from unittest import mock

import pytest

from application_sdk.app._app_config_validation import (
    APP_CONFIG_ERROR_VERSION,
    config_problems,
)
from application_sdk.app.base import App
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.contracts import PreflightGateMode
from application_sdk.templates.sql_app import SqlApp


class _In(Input):
    pass


class _Out(Output):
    pass


def _define_app(**attrs: object) -> type[App]:
    """Define a concrete App subclass whose body declares *attrs*."""

    async def run(self: App, input: _In) -> _Out:
        return _Out()

    run.__annotations__ = {"input": _In, "return": _Out}
    return type("ConfigProbeApp", (App,), {"run": run, **attrs})


def _config_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and f"EntryPointDeclarationError in v{APP_CONFIG_ERROR_VERSION}"
        in str(w.message)
    ]


def _define_and_collect(**attrs: object) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _define_app(**attrs)
    return _config_warnings(caught)


# The ten cases the audit measured: each defined cleanly before this check.
BAD_DECLARATIONS = [
    ("preflight_gate_mode", "hrad"),
    ("artifact_validation_mode", "strict"),
    ("preflight_gate_timeout_seconds", -5),
    ("preflight_gate_max_attempts", 0),
    ("preflight_warmup_ceiling_seconds", "600"),
    ("preflight_verify_storage", "no"),
    ("version", 3),
    ("tags", ["a"]),
    ("passthrough_modules", "pandas"),
    ("name", "Bad Name!"),
]


class TestBadDeclarationsWarn:
    @pytest.mark.parametrize(("attr", "value"), BAD_DECLARATIONS)
    def test_warns_at_class_definition(self, attr: str, value: object) -> None:
        messages = _define_and_collect(**{attr: value})
        assert len(messages) == 1, messages
        assert f"{attr} = {value!r}" in messages[0]

    def test_further_bad_values(self) -> None:
        for attrs in (
            {"preflight_gate_timeout_seconds": 301},
            {"preflight_gate_timeout_seconds": 60.0},
            {"preflight_gate_max_attempts": True},
            {"preflight_warmup_ceiling_seconds": 29},
            {"preflight_warmup_probe_timeout_seconds": 0},
            {"tags": {"k": 1}},
            {"passthrough_modules": {"pandas", 3}},
            {"passthrough_modules": b"pandas"},
            {"name": "app:ep"},
            {"description": None},
            {"preflight_verify_storage": 1},
        ):
            messages = _define_and_collect(**attrs)
            assert len(messages) == 1, (attrs, messages)

    def test_probe_timeout_is_bounded_by_the_effective_ceiling(self) -> None:
        messages = _define_and_collect(
            preflight_warmup_ceiling_seconds=30,
            preflight_warmup_probe_timeout_seconds=45,
        )
        assert len(messages) == 1
        assert "preflight_warmup_probe_timeout_seconds = 45" in messages[0]

    def test_the_message_names_class_problem_and_version(self) -> None:
        (message,) = _define_and_collect(preflight_gate_mode="hrad")
        assert "ConfigProbeApp" in message
        assert "resolves to soft" in message
        assert "v4.0.0" in message

    def test_it_is_also_logged(self) -> None:
        with (
            mock.patch("application_sdk.app._app_config_validation.logger") as log,
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore")
            _define_app(preflight_verify_storage="no")
        log.warning.assert_called_once()
        assert "preflight_verify_storage" in log.warning.call_args.args[1]

    def test_the_warning_points_at_the_class_statement(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            class Probe(App):
                preflight_gate_mode = "hrad"  # type: ignore[assignment]

                async def run(self, input: _In) -> _Out:
                    return _Out()

        (hit,) = [w for w in caught if "preflight_gate_mode" in str(w.message)]
        assert hit.filename == __file__


class TestValidDeclarationsAreSilent:
    @pytest.mark.parametrize(
        "attrs",
        [
            {},
            {"preflight_gate_mode": "hard"},
            {"preflight_gate_mode": " Soft "},
            {"preflight_gate_mode": PreflightGateMode.HARD},
            {"artifact_validation_mode": "hard"},
            {"preflight_gate_timeout_seconds": 5},
            {"preflight_gate_timeout_seconds": 300},
            {"preflight_gate_max_attempts": 3},
            {"preflight_warmup_ceiling_seconds": 1800},
            {"preflight_warmup_probe_timeout_seconds": 30},
            {"preflight_verify_storage": True},
            {"version": "2.0.0"},
            {"tags": {"team": "foundation"}},
            {"tags": None},
            {"passthrough_modules": {"pandas"}},
            {"passthrough_modules": frozenset({"pandas", "numpy"})},
            {"passthrough_modules": None},
            {"name": "csv-ingest-v2"},
            {"name": "PopularityWorkflow"},
            {"name": "my_app.v2"},
        ],
    )
    def test_no_warning(self, attrs: dict[str, object]) -> None:
        assert _define_and_collect(**attrs) == []


class TestOwnDeclarationsOnly:
    def test_an_inherited_bad_value_is_reported_once_at_its_declaration(
        self,
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            class Base(App):
                _app_registered: ClassVar[bool] = True
                preflight_gate_mode = "hrad"  # type: ignore[assignment]

            class Child(Base):
                async def run(self, input: _In) -> _Out:
                    return _Out()

        messages = _config_warnings(caught)
        assert len(messages) == 1
        assert ".Base:" in messages[0]

    def test_config_problems_reads_the_class_dict(self) -> None:
        class Base:
            preflight_gate_mode = "hrad"

        class Child(Base):
            pass

        assert [p[0] for p in config_problems(Base)] == ["preflight_gate_mode"]
        assert config_problems(Child) == []


class TestSandboxReimport:
    def test_skipped_inside_the_workflow_sandbox(self) -> None:
        with mock.patch(
            "application_sdk.app.base.workflow.unsafe.in_sandbox",
            return_value=True,
        ):
            assert _define_and_collect(preflight_gate_mode="hrad") == []


class _Client(BaseSQLClient):
    pass


def _sql_warnings(body: dict[str, Any], base: type = SqlApp) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        type("ProbeSqlApp", (base,), body)
    return [m for m in _config_warnings(caught) if "sql_client_class" in m]


class TestSqlAppClientClass:
    def test_a_concrete_subclass_without_it_warns(self) -> None:
        (message,) = _sql_warnings({})
        assert "sql_client_class = None" in message
        assert "SqlClientClassNotSetError" in message

    def test_a_non_client_class_warns(self) -> None:
        (message,) = _sql_warnings({"sql_client_class": dict})
        assert "is not a BaseSQLClient subclass" in message

    def test_a_client_subclass_is_silent(self) -> None:
        assert _sql_warnings({"sql_client_class": _Client}) == []

    def test_an_inherited_client_is_silent(self) -> None:
        base = type(
            "ProbeSqlBase",
            (SqlApp,),
            {"sql_client_class": _Client, "_app_registered": True},
        )
        assert _sql_warnings({}, base=base) == []

    def test_a_template_is_exempt(self) -> None:
        assert _sql_warnings({"_app_registered": True}) == []

    def test_an_override_of_client_construction_is_exempt(self) -> None:
        async def _init_sql_client(self: SqlApp, input: object) -> BaseSQLClient:
            raise NotImplementedError

        assert _sql_warnings({"_init_sql_client": _init_sql_client}) == []

    def test_an_abstract_base_is_exempt(self) -> None:
        import abc

        @abc.abstractmethod
        def extra(self: SqlApp) -> None: ...

        assert _sql_warnings({"extra": extra}) == []

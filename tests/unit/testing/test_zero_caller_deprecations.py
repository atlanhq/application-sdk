"""The deprecated test surfaces keep serving their names, with a warning, until v4.0.0 (FND-4659).

``testing.full_dag`` has no consumer, and the ``BaseIntegrationTest`` HTTP
scenario framework is superseded by the in-process fixture kit. None of them can be deleted in the release that deprecates them:
the symbol-removal gate only lets a name go once the previous release marked it,
and it reads only ``@deprecated`` and PEP 562 aliases. So every old name here
must still import, resolve to the *same* object as its implementation, and warn
while doing it. A name that stopped resolving would be the ``ImportError`` the
gate exists to prevent; one that resolved silently would give no migration
signal.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings

import pytest

#: public alias module -> the module whose object each name must resolve to.
_ALIAS_MODULES = {
    "application_sdk.testing.full_dag.client": "application_sdk.testing.e2e.client",
    "application_sdk.testing.full_dag._errors": "application_sdk.testing.e2e._errors",
    "application_sdk.testing.full_dag.base": "application_sdk.testing.harness.automation_engine",
    "application_sdk.testing.integration": "application_sdk.testing.integration.runner",
}


def _import_quietly(name: str) -> object:
    # ``full_dag``'s package import warns on its own; that is not under test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return importlib.import_module(name)


def _cases() -> list[tuple[str, str, str]]:
    cases = []
    for public, private in _ALIAS_MODULES.items():
        module = _import_quietly(public)
        for name in module._DEPRECATED_CONSTANTS:  # type: ignore[attr-defined]
            cases.append((public, private, name))
    return cases


@pytest.mark.parametrize(("public", "private", "name"), _cases())
def test_each_old_name_warns_and_is_the_same_object(
    public: str, private: str, name: str
) -> None:
    module = _import_quietly(public)
    implementation = _import_quietly(private)

    with pytest.warns(DeprecationWarning, match=rf"{name} is deprecated.*v4\.0\.0"):
        served = getattr(module, name)

    if name in {"logger", "get_logger"} and public.endswith("full_dag.base"):
        # Served from full_dag.base's own (now private) bindings.
        assert served is getattr(module, f"_{name}")
    else:
        assert served is getattr(implementation, name)


@pytest.mark.parametrize(
    "decorated",
    [
        "application_sdk.testing.full_dag.base:BaseFullDAGE2ETest",
        "application_sdk.testing.full_dag.base:FullDAGOutcome",
        "application_sdk.testing.full_dag.sql_app:SQLAppE2EFullTest",
        "application_sdk.testing.full_dag.payload:ConnectionSpec",
        "application_sdk.testing.full_dag.payload:build_seed_dag",
        "application_sdk.testing.e2e.logs:LogCollector",
    ],
)
def test_definitions_carry_a_gate_visible_marker(decorated: str) -> None:
    """``@deprecated`` sets ``__deprecated__``; the gate reads the decorator."""
    module_name, attr = decorated.split(":")
    obj = getattr(_import_quietly(module_name), attr)

    assert "v4.0.0" in obj.__deprecated__


def test_importing_the_sdr_base_does_not_warn() -> None:
    """The SDK's own SDR module imports the runner directly, not the alias.

    In a subprocess so an earlier import in this session cannot mask it.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import application_sdk.testing.sdr.base",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_star_import_still_binds_the_served_full_dag_names() -> None:
    """``import *`` reads ``__all__``, then resolves each name through ``__getattr__``.

    ``full_dag.base`` had no ``__all__`` before FND-4659, so a star import bound
    its ``logger`` and ``get_logger`` globals. Declaring ``__all__`` without them
    would drop both silently instead of warning.
    """
    module = _import_quietly("application_sdk.testing.full_dag.base")
    namespace: dict[str, object] = {}

    with pytest.warns(DeprecationWarning, match=r"logger is deprecated.*v4\.0\.0"):
        exec("from application_sdk.testing.full_dag.base import *", namespace)  # noqa: S102

    assert namespace["logger"] is module._logger  # type: ignore[attr-defined]
    assert namespace["get_logger"] is module._get_logger  # type: ignore[attr-defined]

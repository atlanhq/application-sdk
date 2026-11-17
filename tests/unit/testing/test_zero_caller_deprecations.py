"""The deprecated test surfaces keep serving their names, with a warning, until v4.0.0 (FND-4659, FND-4911, FND-4292).

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

import contextlib
import importlib
import importlib.util
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

#: public alias module -> the module whose object each name must resolve to.
_ALIAS_MODULES = {
    "application_sdk.testing.full_dag.client": "application_sdk.testing.e2e.client",
    "application_sdk.testing.full_dag._errors": "application_sdk.testing.e2e._errors",
    "application_sdk.testing.full_dag.base": "application_sdk.testing.harness.automation_engine",
    # Each name resolves to whichever submodule ``_LAZY_EXPORTS`` maps it to:
    # the runner's three entry points (FND-4659), and the 46 names of the six
    # modules that exist only to serve it (FND-4911).
    "application_sdk.testing.integration": "",
    # The duplicate SQL client strategy module, merged into clients.sql (FND-4292).
    "application_sdk.testing.hypothesis.strategies.sql_client": (
        "application_sdk.testing.hypothesis.strategies.clients.sql"
    ),
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
    if not private:
        private = importlib.util.resolve_name(
            module._LAZY_EXPORTS[name],  # type: ignore[attr-defined]
            public,
        )
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
        # Imported from the submodule by apps already on the kit, so the package
        # alias alone would never warn them (FND-4911).
        "application_sdk.testing.integration.validation:validate_with_pandera",
        "application_sdk.testing.integration.validation:format_validation_report",
        "application_sdk.testing.integration.validation:get_normalised_dataframe",
        "application_sdk.testing.integration.validation:get_schema_file_paths",
        "application_sdk.testing.integration.client:IntegrationTestClient",
        "application_sdk.testing.hypothesis.strategies.outputs.statestore:credentials_strategy",
        "application_sdk.testing.hypothesis.strategies.outputs.statestore:configuration_strategy",
    ],
)
def test_definitions_carry_a_gate_visible_marker(decorated: str) -> None:
    """``@deprecated`` sets ``__deprecated__``; the gate reads the decorator."""
    module_name, attr = decorated.split(":")
    obj = getattr(_import_quietly(module_name), attr)

    assert "v4.0.0" in obj.__deprecated__


@pytest.mark.parametrize(
    ("target", "call"),
    [
        (
            "application_sdk.testing.integration.validation:validate_with_pandera",
            lambda fn, tmp: fn(str(tmp / "absent"), str(tmp)),
        ),
        (
            "application_sdk.testing.integration.validation:format_validation_report",
            lambda fn, tmp: fn([]),
        ),
        (
            "application_sdk.testing.integration.validation:get_normalised_dataframe",
            lambda fn, tmp: fn(str(tmp)),
        ),
        (
            "application_sdk.testing.integration.validation:get_schema_file_paths",
            lambda fn, tmp: fn(str(tmp)),
        ),
        (
            "application_sdk.testing.integration.client:IntegrationTestClient",
            lambda cls, tmp: cls(host="http://app"),
        ),
    ],
    ids=lambda v: v.rsplit(":", 1)[-1] if isinstance(v, str) else "",
)
def test_direct_submodule_use_warns(
    target: str, call: Callable[[Callable[..., object], Path], object], tmp_path: Path
) -> None:
    """Apps on the kit import these from the submodule, past the package alias.

    The marker test above proves the decorator is there; this proves the
    decorator does what it was added for, on the path those apps take
    (FND-4911). The warning fires before the body runs, so whatever the body
    then does with these throwaway inputs — raise on a missing schema
    directory, or on pandas being absent — is not under test.
    """
    module_name, attr = target.split(":")
    obj = getattr(importlib.import_module(module_name), attr)

    with pytest.warns(DeprecationWarning, match=rf"{attr} is deprecated.*v4\.0\.0"):
        with contextlib.suppress(Exception):
            call(obj, tmp_path)


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


#: Strategy modules nothing imports, deprecated whole (FND-4292). Each old name
#: is served from the module's own underscore-prefixed binding.
_ORPHANED_STRATEGY_MODULES = [
    "application_sdk.testing.hypothesis.strategies.handlers.sql.sql_metadata",
    "application_sdk.testing.hypothesis.strategies.handlers.sql.sql_preflight",
    "application_sdk.testing.hypothesis.strategies.outputs.statestore",
]


def _orphan_cases() -> list[tuple[str, str]]:
    return [
        (module_name, name)
        for module_name in _ORPHANED_STRATEGY_MODULES
        for name in importlib.import_module(module_name)._DEPRECATED_CONSTANTS  # type: ignore[attr-defined]
    ]


@pytest.mark.parametrize(("module_name", "name"), _orphan_cases())
def test_each_orphaned_strategy_warns_and_is_the_same_object(
    module_name: str, name: str
) -> None:
    module = importlib.import_module(module_name)

    with pytest.warns(DeprecationWarning, match=rf"{name} is deprecated.*v4\.0\.0"):
        served = getattr(module, name)

    assert served is getattr(module, f"_{name}")


@pytest.mark.parametrize("name", ["credentials_strategy", "configuration_strategy"])
def test_deprecated_statestore_composites_still_draw(name: str) -> None:
    """The decorated wrappers warn, then hand back a working strategy."""
    from hypothesis import find

    from application_sdk.testing.hypothesis.strategies.outputs import statestore

    with pytest.warns(DeprecationWarning, match=rf"{name} is deprecated.*v4\.0\.0"):
        strategy = getattr(statestore, name)()

    drawn = find(strategy, lambda value: True)
    assert {"username", "password"} <= drawn.keys()


@pytest.mark.parametrize(
    "module_name",
    [
        *_ORPHANED_STRATEGY_MODULES,
        "application_sdk.testing.hypothesis.strategies.sql_client",
    ],
)
def test_star_import_still_binds_every_deprecated_strategy(module_name: str) -> None:
    """``import *`` reads ``__all__`` and resolves each name through ``__getattr__``.

    These names were plain globals before FND-4292, so a star import bound them.
    Without ``__all__`` it would enumerate only the real globals and drop them.
    """
    module = importlib.import_module(module_name)
    namespace: dict[str, object] = {}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        exec(f"from {module_name} import *", namespace)  # noqa: S102

    expected = set(module._DEPRECATED_CONSTANTS)  # type: ignore[attr-defined]
    assert expected <= namespace.keys()
    warned = {str(w.message).split(" ", 1)[0] for w in caught}
    assert expected <= warned

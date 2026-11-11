"""``application_sdk.testing`` and ``testing.e2e`` import lazily (FND-4658).

Generated connector code imports ``CredentialBody`` from
``application_sdk.testing.e2e.credential``, a module that needs only pydantic.
Python runs both package ``__init__`` files on the way there, and while those
imported every public name eagerly the one import cost 2,000+ modules: pytest,
pyatlan, obstore and Temporal. Both inits now serve their names through PEP 562
``__getattr__``; these tests keep it that way.

The imports run in a **subprocess**: in-process, pytest and most of the SDK are
already loaded, so ``sys.modules`` would show them whatever the import pulls in.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

#: Packages none of the probed imports may load. ``_pytest`` is listed beside
#: ``pytest`` because ``pytest`` is a thin re-export of it.
_HEAVY_PACKAGES = frozenset(
    {"pytest", "_pytest", "pyatlan", "pyatlan_v9", "obstore", "temporalio", "grpc"}
)

#: The imports that must stay light: the two package inits, and the module
#: every generated e2e base imports for its credential model.
_LIGHT_IMPORTS = (
    "application_sdk.testing",
    "application_sdk.testing.e2e",
    "application_sdk.testing.e2e.credential",
)


def _loaded_after_importing(module: str) -> set[str]:
    """Import *module* in a fresh interpreter; return the top-level packages loaded."""
    env = dict(os.environ)
    # ``application_sdk.constants`` loads the checkout's ``.env``; whatever the
    # host has there (OTLP export, say) must not decide what gets imported.
    env["PYTHON_DOTENV_DISABLED"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, importlib; importlib.import_module({module!r});"
            "print('\\n'.join(sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr}"
    return {line.split(".")[0] for line in result.stdout.splitlines() if line}


@pytest.mark.parametrize("module", _LIGHT_IMPORTS)
def test_import_loads_no_test_runner_or_runtime_stack(module: str) -> None:
    """Importing *module* loads none of pytest, pyatlan, obstore or Temporal.

    Fix a failure by serving the new name from the package's ``_LAZY`` map
    rather than importing it at the top of ``__init__``.
    """
    offenders = sorted(_loaded_after_importing(module) & _HEAVY_PACKAGES)
    assert not offenders, f"importing {module} loaded: {', '.join(offenders)}"


@pytest.mark.parametrize(
    "package", ["application_sdk.testing", "application_sdk.testing.e2e"]
)
def test_every_declared_name_is_served_lazily(package: str) -> None:
    """``__all__`` and the ``_LAZY`` map agree, and every name resolves.

    A name in ``__all__`` with no ``_LAZY`` entry would fail
    ``from <package> import <name>`` for every consumer.
    """
    pkg = importlib.import_module(package)
    assert set(pkg.__all__) == set(pkg._LAZY)
    for name in pkg.__all__:
        module_name, attr = pkg._LAZY[name]
        expected = getattr(importlib.import_module(module_name), attr)
        assert getattr(pkg, name) is expected, name


@pytest.mark.parametrize(
    "package", ["application_sdk.testing", "application_sdk.testing.e2e"]
)
def test_formerly_eager_submodules_still_resolve_as_attributes(package: str) -> None:
    """``import pkg; pkg.<submodule>`` keeps working for every submodule the
    package used to import eagerly."""
    pkg = importlib.import_module(package)
    for sub in pkg._EAGER_SUBMODULES:
        assert getattr(pkg, sub) is importlib.import_module(f"{package}.{sub}"), sub


@pytest.mark.parametrize(
    "package", ["application_sdk.testing", "application_sdk.testing.e2e"]
)
def test_dir_lists_every_lazy_name_and_formerly_eager_submodule(package: str) -> None:
    """``dir(pkg)`` still shows what the eager init used to bind, before any
    access, so introspection and tab completion see the same surface.

    Fresh interpreter: in-process, other tests have already imported the
    submodules, which binds them as package attributes and hides the gap.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import importlib; print('\\n'.join(dir(importlib.import_module({package!r}))))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    listed = set(result.stdout.splitlines())
    pkg = importlib.import_module(package)
    assert set(pkg._LAZY) <= listed
    assert set(pkg._EAGER_SUBMODULES) <= listed


def test_unknown_names_raise_attribute_error() -> None:
    import application_sdk.testing as testing

    with pytest.raises(AttributeError, match="no attribute 'not_a_name'"):
        _ = testing.not_a_name  # type: ignore[attr-defined]
    assert not hasattr(testing, "not_a_name")

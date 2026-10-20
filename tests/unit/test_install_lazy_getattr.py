"""``lazy_getattr``: the one PEP 562 ``__getattr__`` of the lazy packages.

``application_sdk.contracts``, ``.credentials`` and ``.handler`` import lazily.
Attribute access on them must behave as it did when they imported eagerly:

* a lazy name resolves (and is cached) on first access;
* a lazy name whose module is not installed (an api-only install) is a plain
  ``AttributeError``, so ``hasattr`` / ``getattr(pkg, name, default)`` work;
* a lazy name whose module is installed but broken raises that module's error;
* only the submodules the package used to import eagerly are attributes before
  anything imports them, so ``hasattr(handler, "service")`` is False and does
  not load FastAPI + Temporal, exactly as on main.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from application_sdk._install import lazy_getattr


def _run(code: str) -> None:
    """Run *code* in a fresh interpreter, so no earlier import hides the result."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_hasattr_does_not_import_the_handler_service() -> None:
    _run(
        """
        import sys
        import application_sdk.handler as handler

        assert not hasattr(handler, "service")
        assert "application_sdk.handler.service" not in sys.modules
        assert getattr(handler, "service", None) is None

        from application_sdk.handler import service

        assert handler.service is service
        assert handler.create_app_handler_service is service.create_app_handler_service
        """
    )


def test_dir_lists_the_eager_submodules_before_they_are_imported() -> None:
    """``dir(pkg)`` showed these submodules before the imports went lazy."""
    _run(
        """
        import sys
        import application_sdk.contracts as contracts
        import application_sdk.credentials as credentials

        assert "application_sdk.credentials.git" not in sys.modules
        assert {"atlan", "git", "utils"} <= set(dir(credentials))
        assert {"events", "storage", "types"} <= set(dir(contracts))
        # Listing does not import them.
        assert "application_sdk.credentials.git" not in sys.modules
        """
    )


def test_the_eager_submodules_are_still_attributes() -> None:
    _run(
        """
        import sys
        import application_sdk.contracts as contracts
        import application_sdk.credentials as credentials

        assert contracts.storage.__name__ == "application_sdk.contracts.storage"
        assert hasattr(contracts, "types")
        assert credentials.resolver.__name__ == "application_sdk.credentials.resolver"

        # Not imported eagerly on main either: an attribute only once imported.
        assert not hasattr(credentials, "routing")
        assert "application_sdk.credentials.routing" not in sys.modules
        assert credentials.CredentialMap is not None  # lazy name from routing
        assert hasattr(credentials, "routing")

        from application_sdk.contracts import storage

        assert storage is contracts.storage
        """
    )


# --- the helper itself, over throwaway packages -------------------------------


@pytest.fixture
def pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """``lazyfake`` with a working, a broken and an eager-listed submodule."""
    root = tmp_path / "lazyfake"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "good.py").write_text("VALUE = object()\n")
    (root / "broken.py").write_text("import some_missing_dependency_xyz\n")
    (root / "eager.py").write_text("")
    (root / "other.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.split(".")[0] == "lazyfake"]:
        monkeypatch.delitem(sys.modules, name)
    import lazyfake

    lazy = {
        "VALUE": ("lazyfake.good", "VALUE"),
        "BROKEN": ("lazyfake.broken", "X"),
        "ABSENT": ("lazyfake.not_installed", "X"),
        "ABSENT_PARENT": ("no_such_top_level_pkg.mod", "X"),
    }

    def __getattr__(name: str) -> object:
        return lazy_getattr("lazyfake", name, lazy, vars(lazyfake), {"eager"})

    lazyfake.__getattr__ = __getattr__  # type: ignore[attr-defined]
    yield lazyfake
    for name in [m for m in sys.modules if m.split(".")[0] == "lazyfake"]:
        sys.modules.pop(name, None)


def test_a_lazy_name_resolves_and_is_cached(pkg: ModuleType) -> None:
    value = pkg.VALUE
    assert value is sys.modules["lazyfake.good"].VALUE
    assert vars(pkg)["VALUE"] is value


@pytest.mark.parametrize("name", ["ABSENT", "ABSENT_PARENT"])
def test_a_lazy_name_whose_module_is_not_installed_is_an_attribute_error(
    pkg: ModuleType, name: str
) -> None:
    assert not hasattr(pkg, name)
    assert getattr(pkg, name, "default") == "default"
    with pytest.raises(AttributeError) as raised:
        getattr(pkg, name)
    assert isinstance(raised.value.__cause__, ModuleNotFoundError)


def test_a_lazy_name_whose_module_is_broken_raises_its_own_error(
    pkg: ModuleType,
) -> None:
    with pytest.raises(ModuleNotFoundError) as raised:
        pkg.BROKEN  # noqa: B018
    assert raised.value.name == "some_missing_dependency_xyz"


def test_only_listed_submodules_are_attributes_before_import(pkg: ModuleType) -> None:
    assert pkg.eager.__name__ == "lazyfake.eager"
    assert not hasattr(pkg, "other")
    assert "lazyfake.other" not in sys.modules
    assert not hasattr(pkg, "no_such_submodule")
    with pytest.raises(AttributeError):
        pkg.__wrapped__  # noqa: B018 — dunders never reach a submodule import

"""The retired CI-only tools keep serving their old names until v4.0.0 (FND-4660).

``setup_routes``, the ``scale_data_generator`` package and the parity CLI moved
to private modules. Their public paths are PEP 562 aliases: every name the last
release exported must still import, resolve to the *same* object as its private
home, and warn while doing it. A name that stopped resolving would be the
fleet-wide ``ImportError`` the symbol-removal gate exists to prevent; a name
that resolved silently would give the callers no migration signal.
"""

from __future__ import annotations

import importlib
import runpy
import sys
import warnings

import pytest

#: public module -> its private implementation.
_ALIASES = {
    "application_sdk.testing.setup_routes": "application_sdk.testing._setup_routes",
    "application_sdk.testing.parity.__main__": "application_sdk.testing.parity._cli",
    **{
        f"application_sdk.testing.scale_data_generator.{mod}": (
            f"application_sdk.testing._scale_data_generator.{mod}"
        )
        for mod in (
            "config_loader",
            "data_generator",
            "driver",
            "output_handler.base",
            "output_handler.csv_handler",
            "output_handler.json_handler",
            "output_handler.parquet_handler",
        )
    },
}


def _served_names() -> list[tuple[str, str, str]]:
    cases = []
    for public, private in _ALIASES.items():
        module = importlib.import_module(public)
        for name in module._DEPRECATED_CONSTANTS:
            cases.append((public, private, name))
    return cases


@pytest.mark.parametrize(("public", "private", "name"), _served_names())
def test_each_old_name_warns_and_is_the_same_object(
    public: str, private: str, name: str
) -> None:
    try:
        implementation = importlib.import_module(private)
    except ModuleNotFoundError as exc:
        # The generator's dependencies ship only in extras (faker, pyarrow); a
        # missing SDK module is a real failure.
        if exc.name is None or exc.name.startswith("application_sdk"):
            raise
        pytest.skip(f"optional dependency {exc.name} is not installed")
    module = importlib.import_module(public)

    with pytest.warns(DeprecationWarning, match=rf"{name} is deprecated.*v4\.0\.0"):
        served = getattr(module, name)

    assert served is getattr(implementation, name)


def test_every_setup_routes_export_is_still_served() -> None:
    """The public ``__all__`` of the last release, name for name."""
    private = importlib.import_module("application_sdk.testing._setup_routes")
    public = importlib.import_module("application_sdk.testing.setup_routes")

    assert set(private.__all__) <= set(public._DEPRECATED_CONSTANTS)


def test_an_unknown_name_is_still_an_attribute_error() -> None:
    module = importlib.import_module("application_sdk.testing.setup_routes")

    with pytest.raises(AttributeError):
        module.not_a_name  # noqa: B018


def test_from_import_works_and_warns() -> None:
    """The shape the two connector unit tests that call it actually use."""
    with pytest.warns(DeprecationWarning):
        from application_sdk.testing.setup_routes import read_entrypoints

    from application_sdk.testing._setup_routes import (
        read_entrypoints as implementation,
    )

    assert read_entrypoints is implementation


def test_the_parity_cli_still_runs_and_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``python -m application_sdk.testing.parity`` keeps working until v4.0.0.

    A missing baseline directory is the CLI's input-error exit (2), which proves
    the run reached the real ``main`` without needing comparison fixtures.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["parity", "--baseline", str(tmp_path / "absent"), "--candidate", "x"],
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module(
                "application_sdk.testing.parity", run_name="__main__", alter_sys=False
            )

    assert excinfo.value.code == 2
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "python -m application_sdk.testing.parity is deprecated" in str(w.message)
        for w in caught
    )

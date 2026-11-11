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
import types
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


def _committed_manifest_entries() -> list[dict[str, str]]:
    from pathlib import Path

    import orjson

    root = Path(__file__).resolve().parents[3]
    path = root / "packages/conformance/conformance/data/deprecated_symbols.json"
    return orjson.loads(path.read_bytes())["symbols"]


def test_no_retired_entry_shadows_a_supported_parent_name() -> None:
    """B001 matches an entry against imports from any parent package.

    So an entry for ``parity.__main__.run_comparison`` flags
    ``from application_sdk.testing.parity import run_comparison`` — the very
    replacement its notice recommends. No entry these aliases contribute may
    share a name with something a parent package serves undeprecated.
    """
    retired = (
        "application_sdk.testing.setup_routes",
        "application_sdk.testing.parity.__main__",
        "application_sdk.testing.scale_data_generator",
    )
    collisions = []
    for entry in _committed_manifest_entries():
        module = entry["module"]
        if not module.startswith(retired):
            continue
        parts = module.split(".")
        for depth in range(1, len(parts)):
            parent = importlib.import_module(".".join(parts[:depth]))
            served = getattr(parent, "__dict__", {}).get(entry["symbol"])
            # A submodule bound on its package (``scale_data_generator.driver``)
            # is the retired module itself, not a supported name.
            if served is not None and not isinstance(served, types.ModuleType):
                collisions.append(f"{entry['symbol']}: {module} vs {parent.__name__}")

    assert collisions == []


def test_the_parity_library_names_are_plain_reexports() -> None:
    """The supported comparator API stays importable from ``__main__`` silently."""
    import application_sdk.testing.parity as library
    import application_sdk.testing.parity.__main__ as entry

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for name in ("run_comparison", "generate_markdown", "generate_json_report"):
            assert getattr(entry, name) is getattr(library, name)

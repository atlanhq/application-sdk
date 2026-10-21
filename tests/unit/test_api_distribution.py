"""atlan-application-sdk-api is a listed subset of this tree, released in lockstep.

The api wheel ships the files in ``packages/api/api-files.txt`` for the
consolidated API host; the SDK wheel ships them too and pins the api
distribution to its own version, so the two copies are always the same file.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import application_sdk
from application_sdk._install import api_only_install

ROOT = Path(__file__).resolve().parents[2]


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def test_the_sdk_pins_the_api_distribution_at_its_own_version() -> None:
    sdk = _project(ROOT / "pyproject.toml")
    api = _project(ROOT / "packages/api/pyproject.toml")
    pins = [d for d in sdk["dependencies"] if d.startswith("atlan-application-sdk-api")]
    assert pins == [f"atlan-application-sdk-api=={sdk['version']}"]
    assert api["version"] == sdk["version"] == application_sdk.__version__


def test_the_api_core_dependencies_match_the_sdks_pins() -> None:
    """The api wheel's own dependencies carry the SDK's pins, so the host and the
    worker resolve the same versions (FastAPI moved to 0.142 on main once and the
    api package was left behind)."""
    sdk = {
        re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0]: re.sub(r"\[[^\]]*\]", "", r)
        for r in _project(ROOT / "pyproject.toml")["dependencies"]
    }
    # A transitive dependency of the SDK carries its constraint pin (the CVE
    # floors in [tool.uv].constraint-dependencies) instead.
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for r in root["tool"]["uv"].get("constraint-dependencies", []):
        sdk.setdefault(re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0], r)
    for req in _project(ROOT / "packages/api/pyproject.toml")["dependencies"]:
        name = re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0]
        assert sdk.get(name) == req, (name, sdk.get(name), req)


def _name(req: str) -> str:
    return re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0]


def _sdk_pins_for_extra(sdk: dict, extra: str) -> dict[str, str]:
    """What ``atlan-application-sdk[extra]`` pins: its core dependencies plus
    that one extra's (never another extra's). A name pinned differently in the
    core and in the extra has no single answer, so it fails here."""
    pins = {_name(r): r for r in sdk["dependencies"]}
    for req in sdk.get("optional-dependencies", {}).get(extra, []):
        name = _name(req)
        assert pins.setdefault(name, req) == req, (extra, name, pins[name], req)
    return pins


def test_the_api_extras_match_the_sdks_pins() -> None:
    """A handler declares the same extra on either install and gets the same pin.

    Compared extra by extra: merging every SDK extra into one map would let a
    package pinned differently in two extras silently take whichever came last.
    """
    sdk = _project(ROOT / "pyproject.toml")
    api = _project(ROOT / "packages/api/pyproject.toml")
    for extra, reqs in api["optional-dependencies"].items():
        pins = _sdk_pins_for_extra(sdk, extra)
        for req in reqs:
            name = _name(req)
            assert pins.get(name) == req, (extra, name, pins.get(name), req)


def test_extra_pins_are_read_from_that_extra_only() -> None:
    sdk = {
        "dependencies": ["httpx>=0.27,<0.29"],
        "optional-dependencies": {"a": ["pandas>=2,<3"], "b": ["pandas>=1,<2"]},
    }
    assert _sdk_pins_for_extra(sdk, "a")["pandas"] == "pandas>=2,<3"
    assert _sdk_pins_for_extra(sdk, "b")["pandas"] == "pandas>=1,<2"
    assert "pandas" not in _sdk_pins_for_extra(sdk, "absent")
    assert _sdk_pins_for_extra(sdk, "absent")["httpx"] == "httpx>=0.27,<0.29"


def test_the_two_entrypoint_module_segment_copies_agree() -> None:
    """``handler/routes.py`` keeps its own copy (the api wheel cannot import
    ``application_sdk.app``); the two must convert every name the same way."""
    from application_sdk.app.entrypoint import entrypoint_module_segment as canonical
    from application_sdk.handler.routes import entrypoint_module_segment as copy

    for name in (
        "",
        "extract",
        "extract-metadata",
        "asset-export-advanced",
        "already_snake",
        "mixed-kebab_snake",
        "--double--",
        "-leading",
        "trailing-",
        "UPPER-Case",
        "v2-api-3",
        "dots.stay.dots",
        "ünïcode-näme",
    ):
        assert copy(name) == canonical(name), name


def test_every_listed_file_is_part_of_the_sdk_package() -> None:
    lines = (ROOT / "packages/api/api-files.txt").read_text().splitlines()
    listed = [ln for ln in lines if ln and not ln.startswith("#")]
    assert listed == sorted(set(listed)), "keep api-files.txt sorted and unique"
    for rel in listed:
        assert rel.startswith("application_sdk/") and (ROOT / rel).is_file(), rel


def test_the_sdk_env_is_not_an_api_only_install() -> None:
    """Guards take their api-only fallback only on the host, never in the worker."""
    assert api_only_install() is False


def test_lazy_package_names_are_the_objects_they_always_were() -> None:
    from application_sdk.contracts import UploadInput
    from application_sdk.contracts.storage import UploadInput as defined
    from application_sdk.credentials import CredentialRef
    from application_sdk.credentials.ref import CredentialRef as ref_defined
    from application_sdk.handler import create_app_handler_service
    from application_sdk.handler.service import create_app_handler_service as svc

    assert UploadInput is defined
    assert CredentialRef is ref_defined
    assert create_app_handler_service is svc


def test_moved_classes_keep_their_old_import_paths() -> None:
    from application_sdk._context_errors import (
        AppContextError,
        ObjectStoreNotConfiguredError,
        SecretStoreNotConfiguredError,
    )
    from application_sdk.app.base import AppContextError as a
    from application_sdk.app.base_errors import ObjectStoreNotConfiguredError as o
    from application_sdk.app.base_errors import SecretStoreNotConfiguredError as s
    from application_sdk.common.concurrency import run_in_thread
    from application_sdk.contracts.base import MaxItems
    from application_sdk.contracts.types import MaxItems as types_max
    from application_sdk.credentials.extra import parse_credentials_extra
    from application_sdk.credentials.utils import parse_credentials_extra as utils_pce
    from application_sdk.execution.heartbeat import run_in_thread as heartbeat_rit

    assert (a, o, s) == (
        AppContextError,
        ObjectStoreNotConfiguredError,
        SecretStoreNotConfiguredError,
    )
    assert MaxItems is types_max
    assert parse_credentials_extra is utils_pce
    assert run_in_thread is heartbeat_rit


def test_a_lazy_package_still_exposes_its_submodules_as_attributes() -> None:
    """``import pkg; pkg.sub`` keeps working after the package's imports went lazy."""
    import application_sdk.contracts as contracts
    import application_sdk.credentials as credentials
    import application_sdk.handler as handler

    assert contracts.storage.__name__ == "application_sdk.contracts.storage"
    assert handler.contracts.__name__ == "application_sdk.handler.contracts"
    assert credentials.spec.__name__ == "application_sdk.credentials.spec"
    with pytest.raises(AttributeError):
        contracts.no_such_submodule  # noqa: B018


def test_the_worker_entry_point_names_the_fix_when_the_api_files_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pip upgrade across the split deletes the api distribution's files; the
    worker entry point then says how to recover instead of a bare import error."""
    import importlib
    import importlib.util
    import sys

    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: object, **kwargs: object):
        if name == "application_sdk.handler.base":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.delitem(sys.modules, "application_sdk.main", raising=False)
    with pytest.raises(
        ImportError,
        match="pip install --force-reinstall --no-deps atlan-application-sdk-api",
    ):
        importlib.import_module("application_sdk.main")


@pytest.mark.parametrize(
    "package",
    [
        "application_sdk.contracts",
        "application_sdk.credentials",
        "application_sdk.handler",
    ],
)
def test_a_lazy_name_defined_in_a_core_file_loads_through_a_core_file(
    package: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """On a core-only api install, ``pkg.name`` must not detour through a file
    that needs an extra when the object itself is defined in a core file."""
    import importlib
    import sys

    # The script puts its own directory on sys.path and imports a sibling, so
    # give it a private copy of the path and drop what it registered after.
    monkeypatch.setattr(sys, "path", [str(ROOT / ".github/scripts"), *sys.path])
    for name in {"gen_api_files", "check_api_surface"} - sys.modules.keys():
        request.addfinalizer(lambda name=name: sys.modules.pop(name, None))
    import gen_api_files  # noqa: PLC0415

    groups = gen_api_files.file_groups(ROOT)

    def group_of(module: str) -> set[str]:
        base = module.replace(".", "/")
        for rel in (f"{base}/__init__.py", f"{base}.py"):
            if rel in groups:
                return groups[rel]
        return set()

    pkg = importlib.import_module(package)
    detours = []
    for name, (target, _attr) in pkg._LAZY.items():  # noqa: SLF001
        defined_in = getattr(getattr(pkg, name), "__module__", "") or ""
        if "core" in group_of(defined_in) and "core" not in group_of(target):
            detours.append(f"{name}: via {target}, defined in {defined_in}")
    assert detours == []

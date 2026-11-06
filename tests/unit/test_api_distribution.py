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
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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


def _python_classifiers(project: dict) -> list[str]:
    prefix = "Programming Language :: Python :: 3."
    return [
        c.removeprefix("Programming Language :: Python :: ")
        for c in project["classifiers"]
        if c.startswith(prefix)
    ]


def test_the_three_packages_declare_the_python_versions_ci_tests() -> None:
    """The SDK, api and conformance packages are released from one tree and
    tested on one matrix, so they claim the same Python versions: that matrix.
    They had drifted to 3.11 only (api), 3.11-3.13 (SDK) and 3.11-3.14
    (conformance) while CI tested all three on 3.11-3.14."""
    workflow = (ROOT / ".github/workflows/sdk-tests-reusable.yaml").read_text(
        encoding="utf-8"
    )
    matrices = re.findall(r"^\s+python-version: \[([^\]]*)\]", workflow, re.MULTILINE)
    assert len(matrices) == 1, matrices
    tested = re.findall(r'"([^"]+)"', matrices[0])
    assert tested, matrices[0]
    for path in (
        "pyproject.toml",
        "packages/api/pyproject.toml",
        "packages/conformance/pyproject.toml",
    ):
        project = _project(ROOT / path)
        assert _python_classifiers(project) == tested, path
        assert project["requires-python"] == f">={tested[0]}", path


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


def _lower_bound(req: str) -> str | None:
    """The version of ``req``'s one ``>=`` specifier; None if it has none or
    several (which leave no single floor to compare)."""
    floors = [s.version for s in Requirement(req).specifier if s.operator == ">="]
    return floors[0] if len(floors) == 1 else None


def _constraint_floor_mismatches(root: dict, api: dict) -> list[str]:
    """Overlapping dependencies whose floor differs from their CVE constraint.

    A package that both the SDK and the api package declare (core or any extra)
    and that has a ``[tool.uv] constraint-dependencies`` line must carry exactly
    that line's floor in every requirement on both sides. A missing or
    ambiguous floor (no ``>=``, or more than one) on either side is a
    mismatch: it carries no CVE floor to agree on.
    """
    sdk = root["project"]

    def declared(project: dict) -> dict[str, list[str]]:
        reqs: dict[str, list[str]] = {}
        groups = [project["dependencies"]]
        groups += (project.get("optional-dependencies") or {}).values()
        for req in (r for group in groups for r in group):
            reqs.setdefault(canonicalize_name(Requirement(req).name), []).append(req)
        return reqs

    sdk_reqs, api_reqs = declared(sdk), declared(api)
    problems = []
    for line in root["tool"]["uv"].get("constraint-dependencies", []):
        name = canonicalize_name(Requirement(line).name)
        if name not in sdk_reqs or name not in api_reqs:
            continue
        floor = _lower_bound(line)
        if floor is None:
            problems.append(f"constraint {line!r} has no single >= floor")
            continue
        for where, req in [("sdk", r) for r in sdk_reqs[name]] + [
            ("api", r) for r in api_reqs[name]
        ]:
            if _lower_bound(req) != floor:
                problems.append(f"{where} {req!r} != constraint {line!r}")
    return problems


def test_overlapping_dependencies_carry_their_constraint_floor() -> None:
    """A CVE fix on a package the SDK and the api package share moves its
    constraint, the SDK's pin and the api's pin together, to the same floor.

    The tests above compare the api's pins with the SDK's and fall back to the
    constraint only for a package the SDK does not publish, so a fix that raised
    the constraint and left both pins behind (or the reverse) passed them. Fix a
    failure by giving the constraint line and every SDK and api requirement for
    that package the same ``>=`` floor.
    """
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    api = _project(ROOT / "packages/api/pyproject.toml")
    assert _constraint_floor_mismatches(root, api) == []


def test_constraint_floor_mismatches_catches_a_one_sided_fix() -> None:
    root = {
        "project": {
            "dependencies": ["authlib>=1.9.4,<2", "httpx>=0.27,<0.29"],
            "optional-dependencies": {"sql": ["python-dotenv>=1.2.2,<2"]},
        },
        "tool": {
            "uv": {
                "constraint-dependencies": [
                    "authlib>=1.9.4",
                    "python-dotenv>=1.2.3",  # raised here only
                    "filelock>=3.20.1",  # not shared: ignored
                ]
            }
        },
    }
    api = {
        "dependencies": ["httpx>=0.27,<0.29"],
        "optional-dependencies": {
            "pyatlan": ["authlib>=1.9.1,<2"],  # left behind
            "sql": ["python-dotenv>=1.2.2,<2"],
        },
    }
    assert _constraint_floor_mismatches(root, api) == [
        "api 'authlib>=1.9.1,<2' != constraint 'authlib>=1.9.4'",
        "sdk 'python-dotenv>=1.2.2,<2' != constraint 'python-dotenv>=1.2.3'",
        "api 'python-dotenv>=1.2.2,<2' != constraint 'python-dotenv>=1.2.3'",
    ]


def test_constraint_floor_mismatches_rejects_missing_and_ambiguous_floors() -> None:
    """Bare or doubly-floored requirements carry no single CVE floor. Before,
    both sides collapsed to None and compared equal, so nothing was reported."""
    root = {
        "project": {
            "dependencies": ["authlib", "httpx>=0.27,>=0.28,<0.29"],
        },
        "tool": {
            "uv": {
                "constraint-dependencies": [
                    "authlib",  # no floor at all
                    "httpx>=0.28",
                ]
            }
        },
    }
    api = {"dependencies": ["authlib", "httpx>=0.28,<0.29"]}
    assert _constraint_floor_mismatches(root, api) == [
        "constraint 'authlib' has no single >= floor",
        "sdk 'httpx>=0.27,>=0.28,<0.29' != constraint 'httpx>=0.28'",
    ]


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

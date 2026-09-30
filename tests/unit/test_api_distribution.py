"""atlan-application-sdk-api is a listed subset of this tree, released in lockstep.

The api wheel ships the files in ``packages/api/api-files.txt`` for the
consolidated API host; the SDK wheel ships them too and pins the api
distribution to its own version, so the two copies are always the same file.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

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


def test_the_api_extras_match_the_sdks_pins() -> None:
    """A handler declares the same extra on either install and gets the same pin."""
    sdk_reqs = {
        re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0]: r
        for reqs in _project(ROOT / "pyproject.toml")["optional-dependencies"].values()
        for r in reqs
    } | {
        re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0]: r
        for r in _project(ROOT / "pyproject.toml")["dependencies"]
    }
    for reqs in _project(ROOT / "packages/api/pyproject.toml")[
        "optional-dependencies"
    ].values():
        for req in reqs:
            name = re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0]
            assert sdk_reqs.get(name) == req, (name, sdk_reqs.get(name), req)


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

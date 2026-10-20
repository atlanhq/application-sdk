"""``worker_only_missing`` falls back only for a worker-only module on the host.

A guard ``except ModuleNotFoundError as exc: if not worker_only_missing(exc):
raise`` must take its api-only fallback for the worker modules it wraps, and
re-raise every other missing module, so a missing api file or third-party
dependency on the host is a real error rather than a silent fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk import _install
from application_sdk._install import _WORKER_ONLY_PREFIXES, worker_only_missing

ROOT = Path(__file__).resolve().parents[2]


def _missing(name: str | None) -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


@pytest.fixture
def api_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the API host: only the api distribution is installed.

    Patches the module global (not ``find_spec``), so the cached real answer is
    never replaced and nothing leaks into other tests on this worker.
    """
    monkeypatch.setattr(_install, "api_only_install", lambda: True)


@pytest.mark.parametrize(
    "name",
    [
        "temporalio",
        "temporalio.workflow",
        "application_sdk.storage",
        "application_sdk.storage.ops",
        "application_sdk.infrastructure",
        "application_sdk.infrastructure.context",
        "application_sdk.observability",
        "application_sdk.observability.logger_adaptor",
    ],
)
@pytest.mark.usefixtures("api_only")
def test_a_worker_only_module_takes_the_fallback_on_the_host(name: str) -> None:
    assert worker_only_missing(_missing(name)) is True


@pytest.mark.parametrize(
    "name",
    [
        "some_thirdparty",
        "boto3",
        "temporalio_extra",  # a prefix of the name, not the module or a parent
        "application_sdk.storage_helpers",
        "application_sdk.handler.base",  # shipped by the api distribution
        "application_sdk.contracts.storage",
        "application_sdk",
        None,
    ],
)
@pytest.mark.usefixtures("api_only")
def test_any_other_missing_module_still_raises_on_the_host(name: str | None) -> None:
    exc = _missing(name)
    with pytest.raises(ModuleNotFoundError) as raised:
        try:
            raise exc
        except ModuleNotFoundError as caught:
            if not worker_only_missing(caught):
                raise
    assert raised.value is exc


@pytest.mark.parametrize(
    "name", ["temporalio", "application_sdk.storage.ops", "some_thirdparty"]
)
def test_on_the_full_sdk_it_is_always_false(name: str) -> None:
    assert _install.api_only_install() is False
    assert worker_only_missing(_missing(name)) is False


def test_no_prefix_covers_a_file_the_api_distribution_ships() -> None:
    """A listed module missing on the host is a broken install, never a fallback."""
    lines = (ROOT / "packages/api/api-files.txt").read_text().splitlines()
    for rel in (ln for ln in lines if ln and not ln.startswith("#")):
        module = rel.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        for prefix in _WORKER_ONLY_PREFIXES:
            assert not (module == prefix or module.startswith(prefix + ".")), (
                module,
                prefix,
            )

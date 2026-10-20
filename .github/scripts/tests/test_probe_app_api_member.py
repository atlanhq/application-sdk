"""Tests for probe_app_api_member.py — the host matrix an app's api member must pass."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import probe_app_api_member as probe  # noqa: E402

_HANDLER = """
from application_sdk.handler import Handler
from application_sdk.handler.contracts import AuthInput, AuthOutput, MetadataInput, MetadataOutput, PreflightInput, PreflightOutput


class DemoHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(success=True)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        {preflight}

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[])


handler = DemoHandler()
"""


def _member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight: str) -> str:
    package = f"probe_demo_{abs(hash(preflight))}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "__init__.py").write_text(
        textwrap.dedent(_HANDLER).format(preflight=preflight)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return package


def _problems(result: probe.Result) -> list[str]:
    # The SDK's dev env has the full SDK installed; that one is expected here.
    return [p for p in result.problems if "worker SDK" not in p]


def test_a_worker_only_import_in_preflight_fails_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _member(
        tmp_path, monkeypatch, "import application_sdk_no_such_worker_module"
    )
    problems = _problems(probe.run("demo", package, "handler"))
    assert any(
        "/workflows/v1/check" in p and "worker-only import" in p for p in problems
    ), problems


def test_a_handler_that_fails_for_want_of_a_source_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _member(tmp_path, monkeypatch, "raise ConnectionError('no source')")
    assert _problems(probe.run("demo", package, "handler")) == []


class _Answer:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "{}"


class _NoConfigmaps:
    """A host that serves no contract files: every metadata route is a 404."""

    def get(self, path: str) -> _Answer:
        return _Answer(404)

    def post(self, path: str, json: object = None) -> _Answer:
        return _Answer(404)


def test_a_404_is_a_pass_when_the_app_ships_no_contract_files() -> None:
    result = probe.Result()
    probe.check_metadata_routes(_NoConfigmaps(), result, expects_contracts=False)
    assert result.problems == []


def test_a_404_fails_when_the_app_ships_contract_files() -> None:
    """The files exist in the source but not in the wheel: the host would serve
    no setup form, and the manifest and input-contract checks must not be skipped."""
    result = probe.Result()
    probe.check_metadata_routes(_NoConfigmaps(), result, expects_contracts=True)
    assert len(result.problems) == 1, result.problems
    assert "configmaps" in result.problems[0]


def test_contract_files_are_read_from_the_apps_generated_dir(tmp_path: Path) -> None:
    assert probe.ships_contracts(tmp_path / "app/generated") is False
    (tmp_path / "app/generated").mkdir(parents=True)
    assert probe.ships_contracts(tmp_path / "app/generated") is False
    (tmp_path / "app/generated/demo.json").write_text("{}")
    assert probe.ships_contracts(tmp_path / "app/generated") is True

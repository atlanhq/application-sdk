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

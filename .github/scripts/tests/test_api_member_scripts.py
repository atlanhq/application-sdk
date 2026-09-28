"""Tests for detect_api_member.py and probe_app_api_member.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import detect_api_member as detect  # noqa: E402
import probe_app_api_member as probe  # noqa: E402

_MEMBER = """
[project]
name = "atlan-mysql-api"

[project.entry-points."atlan.app_api"]
mysql = "atlan_mysql_api:handler"
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_no_entry_point_is_not_present(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "atlan-publish-app"\n')
    assert detect.outputs(detect.find_members(tmp_path)) == ["present=false"]


def test_the_api_member_is_found(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "atlan-mysql-app"\n')
    _write(tmp_path, "api/pyproject.toml", _MEMBER)
    assert detect.outputs(detect.find_members(tmp_path)) == [
        "present=true",
        "member=api",
        "name=mysql",
        "package=atlan_mysql_api",
        "object=handler",
    ]


def test_venvs_and_tests_are_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, ".venv/lib/x/pyproject.toml", _MEMBER)
    _write(tmp_path, "tests/fixtures/pyproject.toml", _MEMBER)
    assert detect.outputs(detect.find_members(tmp_path)) == ["present=false"]


def test_two_entry_points_are_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "api/pyproject.toml", _MEMBER)
    _write(tmp_path, "api2/pyproject.toml", _MEMBER.replace("mysql =", "other ="))
    with pytest.raises(ValueError, match="more than one"):
        detect.outputs(detect.find_members(tmp_path))


def test_the_closure_check_flags_the_worker_sdk() -> None:
    result = probe.Result()
    probe.check_import_closure(
        {"application_sdk_api.errors", "application_sdk.clients.sql"}, result
    )
    assert result.problems and "application_sdk.clients.sql" in result.problems[0]


def test_the_closure_check_passes_the_api_package() -> None:
    result = probe.Result()
    probe.check_import_closure({"application_sdk_api.errors", "fastapi"}, result)
    assert result.problems == []


def test_a_bare_500_fails_and_a_json_500_passes() -> None:
    bare = probe.Result()
    probe.check_response("auth", 500, None, bare, not_500_json=True)
    assert bare.problems
    json_500 = probe.Result()
    probe.check_response("auth", 500, {"success": False}, json_500, not_500_json=True)
    assert json_500.problems == []


def test_a_status_mismatch_fails() -> None:
    result = probe.Result()
    probe.check_response("health", 503, None, result, want=200)
    assert result.problems == ["health: expected HTTP 200, got 503"]

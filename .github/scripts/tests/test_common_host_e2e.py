"""Tests for the common-app-server half of a hosted app's e2e run (FND-5377)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common_host_e2e as host

A, B = "a" * 40, "b" * 40
VERSION_ID = "019feaef-02a2-76d3-9c87-78a5fb40515c"


@pytest.mark.parametrize(
    ("text", "hosted"),
    [
        ("deploy:\n  routeToCommonAPIServer: true\n", True),
        ("deploy:\n  routeToCommonAPIServer: false\n", False),
        ("deploy:\n  routeToCommonAPIServer: 'true'\n", False),
        ("deploy:\n  env: {}\n", False),
        ("name: x\n", False),
        ("", False),
    ],
)
def test_detect_reads_the_deploy_flag(tmp_path: Path, text: str, hosted: bool) -> None:
    path = tmp_path / "atlan.yaml"
    path.write_text(text)
    assert host.is_hosted(path) is hosted


def test_detect_without_atlan_yaml_is_not_hosted(tmp_path: Path) -> None:
    assert host.is_hosted(tmp_path / "atlan.yaml") is False


def test_detect_refuses_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "atlan.yaml"
    path.write_text("deploy: [unclosed\n")
    with pytest.raises(host.CommonHostError):
        host.is_hosted(path)


def test_the_image_names_the_app_its_commit_and_the_host_commit() -> None:
    assert host.host_image("ai-memory", A, B) == (
        "ghcr.io/atlanhq/common-app-server:e2e-ai-memory-aaaaaaa-bbbbbbb",
        "e2e-ai-memory-aaaaaaa-bbbbbbb",
    )


@pytest.mark.parametrize(
    ("key", "app_rev", "host_rev"),
    [("Bad_Key", A, B), ("ai-memory", "main", B), ("ai-memory", A, "b" * 39)],
)
def test_the_image_refuses_bad_input(key: str, app_rev: str, host_rev: str) -> None:
    with pytest.raises(host.CommonHostError):
        host.host_image(key, app_rev, host_rev)


def test_main_prints_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        host.main(["image", "--app-key", "playbook", "--app-rev", A, "--host-rev", B])
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        "image=ghcr.io/atlanhq/common-app-server:e2e-playbook-aaaaaaa-bbbbbbb",
        "version=e2e-playbook-aaaaaaa-bbbbbbb",
    ]
    assert (
        host.main(["image", "--app-key", "x", "--app-rev", "main", "--host-rev", B])
        == 1
    )


def test_hosted_repos_lists_the_pinned_app_repos(tmp_path: Path) -> None:
    pins = tmp_path / "pins.toml"
    pins.write_text(
        '[sdk]\nrepo = "atlanhq/application-sdk"\n\n[apps]\n'
        '[apps.ai-memory]\nrepo = "atlanhq/atlan-memory-app"\n'
        '[apps.playbook]\nrepo = "atlanhq/atlan-playbook-app"\n'
    )
    assert host.hosted_repos(pins) == ["atlan-memory-app", "atlan-playbook-app"]


@pytest.mark.parametrize(
    "text",
    [
        "[apps]\n",
        '[apps.x]\nrepo = "someone/else"\n',
        "nope = [",
        '[sdk]\nrepo = "a"\n',
    ],
)
def test_hosted_repos_refuses_an_empty_or_foreign_list(
    tmp_path: Path, text: str
) -> None:
    pins = tmp_path / "pins.toml"
    pins.write_text(text)
    with pytest.raises(host.CommonHostError):
        host.hosted_repos(pins)


def test_the_e2e_host_app_id_comes_from_the_host_repo(tmp_path: Path) -> None:
    path = tmp_path / "e2e-host.toml"
    path.write_text(f'app_id = "{VERSION_ID}"\n')
    assert host.e2e_host_app_id(path) == VERSION_ID


@pytest.mark.parametrize(
    "text", ['app_id = "not-a-uuid"\n', "other = 1\n", "app_id = [", "app_id = 5\n"]
)
def test_a_bad_e2e_host_file_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "e2e-host.toml"
    path.write_text(text)
    with pytest.raises(host.CommonHostError):
        host.e2e_host_app_id(path)


def test_a_missing_e2e_host_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(host.CommonHostError, match="cannot read"):
        host.e2e_host_app_id(tmp_path / "e2e-host.toml")


def test_main_prints_the_e2e_host_app_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "e2e-host.toml"
    path.write_text(f'app_id = "{VERSION_ID}"\n')
    assert host.main(["e2e-host", "--file", str(path)]) == 0
    assert capsys.readouterr().out.strip() == f"app_id={VERSION_ID}"
    assert host.main(["e2e-host", "--file", str(tmp_path / "missing.toml")]) == 1


def test_the_shared_hosts_own_app_id_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "e2e-host.toml"
    path.write_text(f'app_id = "{VERSION_ID}"\n')
    shared = tmp_path / "atlan.yaml"
    shared.write_text(f'name: common-app-server\napp_id: "{VERSION_ID}"\n')
    with pytest.raises(host.CommonHostError, match="shared common-app-server"):
        host.e2e_host_app_id(path, shared)
    shared.write_text(
        'name: common-app-server\napp_id: "01a2119d-0000-7000-8000-000000000001"\n'
    )
    assert host.e2e_host_app_id(path, shared) == VERSION_ID


def test_a_shared_atlan_yaml_without_an_app_id_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "e2e-host.toml"
    path.write_text(f'app_id = "{VERSION_ID}"\n')
    shared = tmp_path / "atlan.yaml"
    shared.write_text("name: common-app-server\n")
    with pytest.raises(host.CommonHostError, match="has no app_id"):
        host.e2e_host_app_id(path, shared)

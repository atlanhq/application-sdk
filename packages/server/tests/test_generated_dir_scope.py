"""ATLAN_CONTRACT_GENERATED_DIR is process-global; the host serves eight apps.

One value cannot be right for eight apps, and the quiet failure is the worst
one: a value that EXISTS but belongs to another app makes this app answer 200
with that connector's setup form and manifest. So the override is honoured only
when the process is that app — which the host's own ATLAN_APPLICATION_NAME
pinning makes an exact test, since the host sets it to its own name.
"""

from __future__ import annotations

import logging

import pytest
from server_sdk.server import _default_generated_dir


@pytest.fixture
def other_app_dir(tmp_path, monkeypatch):
    d = tmp_path / "mysql_generated"
    d.mkdir()
    monkeypatch.setenv("ATLAN_CONTRACT_GENERATED_DIR", str(d))
    return d


def test_standalone_honours_the_override(other_app_dir, monkeypatch) -> None:
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "redshift")
    assert _default_generated_dir("redshift") == other_app_dir


def test_the_host_refuses_it(other_app_dir, monkeypatch, caplog) -> None:
    """The host pins ATLAN_APPLICATION_NAME to its OWN name, never a hosted
    app's, so this is an exact standalone-vs-hosted test."""
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "common-api-server")
    with caplog.at_level(logging.WARNING):
        got = _default_generated_dir("redshift")
    assert got != other_app_dir
    assert "would serve another app's forms" in caplog.text
    assert "redshift" in caplog.text


def test_an_unset_application_name_is_also_refused(other_app_dir, monkeypatch) -> None:
    monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
    assert _default_generated_dir("redshift") != other_app_dir


def test_a_nonexistent_override_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAN_CONTRACT_GENERATED_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "redshift")
    assert _default_generated_dir("redshift") != tmp_path / "nope"


def test_an_empty_override_never_becomes_the_cwd(monkeypatch) -> None:
    """Path("") is the CWD, and the configmap routes would then walk the whole
    tree under it and serve every co-hosted app's forms."""
    from pathlib import Path

    monkeypatch.setenv("ATLAN_CONTRACT_GENERATED_DIR", "   ")
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "redshift")
    assert _default_generated_dir("redshift") != Path("")

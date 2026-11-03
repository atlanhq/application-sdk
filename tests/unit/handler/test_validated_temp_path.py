"""Unit tests for the temp-path guard used by the file and config endpoints.

There is one guard, :func:`application_sdk.handler.object_routes.validated_temp_path`,
shared by the worker's handler service and the consolidated API host.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from application_sdk.handler import object_routes, service
from application_sdk.handler.object_routes import validated_temp_path
from application_sdk.handler.service_errors import TempPathEscapeError


def test_the_worker_and_the_host_share_one_guard() -> None:
    assert service._validated_temp_path is object_routes.validated_temp_path


def test_mkstemp_file_inside_temp_dir_is_returned_resolved() -> None:
    fd, path = tempfile.mkstemp(dir=tempfile.gettempdir())
    os.close(fd)
    try:
        assert validated_temp_path(path) == os.path.realpath(path)
    finally:
        os.unlink(path)


def test_temp_dir_itself_is_rejected() -> None:
    with pytest.raises(TempPathEscapeError):
        validated_temp_path(tempfile.gettempdir())


def test_path_outside_temp_dir_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))

    with pytest.raises(TempPathEscapeError):
        validated_temp_path(str(tmp_path / "outside.txt"))


def test_dotdot_traversal_out_of_temp_dir_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))

    with pytest.raises(TempPathEscapeError):
        validated_temp_path(str(root / "a" / ".." / ".." / "outside.txt"))

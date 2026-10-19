"""Unit tests for the temp-path guard used by the handler file endpoint."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from application_sdk.handler.service import _validated_temp_path
from application_sdk.handler.service_errors import TempPathEscapeError


def test_mkstemp_file_inside_temp_dir_is_returned_resolved() -> None:
    fd, path = tempfile.mkstemp(dir=tempfile.gettempdir())
    os.close(fd)
    try:
        assert _validated_temp_path(path) == os.path.realpath(path)
    finally:
        os.unlink(path)


def test_temp_dir_itself_is_rejected() -> None:
    with pytest.raises(TempPathEscapeError):
        _validated_temp_path(tempfile.gettempdir())


def test_path_outside_temp_dir_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))

    with pytest.raises(TempPathEscapeError):
        _validated_temp_path(str(tmp_path / "outside.txt"))


def test_dotdot_traversal_out_of_temp_dir_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))

    with pytest.raises(TempPathEscapeError):
        _validated_temp_path(str(root / "a" / ".." / ".." / "outside.txt"))

"""Regression coverage for asynchronous atomic writes."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common.atomic import async_atomic_write
from application_sdk.errors import DiskFullError


async def test_async_atomic_write_types_enospc_from_staging_file_creation(
    tmp_path: Path,
) -> None:
    """A full filesystem during mkstemp must produce the SDK's typed error."""
    artifact = tmp_path / "artifact.bin"

    with (
        patch(
            "application_sdk.common.atomic.tempfile.mkstemp",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ),
        pytest.raises(DiskFullError) as caught,
    ):
        async with async_atomic_write(artifact, operation="async test write"):
            pass

    assert caught.value.operation == "async test write"
    assert caught.value.path == str(artifact)
    assert not artifact.exists()

"""The incremental modules' deprecated re-exports keep working, and warn.

Functions are ``@deprecated`` wrappers (they warn when called, and the
conformance scan records them as functions); classes stay behind the module
``__getattr__`` so ``except`` still catches the SDK's own error type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk._runtime import offload
from application_sdk.common import atomic
from application_sdk.common.incremental import helpers, marker
from application_sdk.common.incremental.state import state_reader
from application_sdk.storage import errors


def test_function_aliases_warn_on_call_and_delegate(tmp_path: Path) -> None:
    with pytest.warns(DeprecationWarning, match="get_persistent_artifacts_path"):
        path = marker.get_persistent_artifacts_path("default/x/1", "state", "app")
    assert path == helpers.get_persistent_artifacts_path("default/x/1", "state", "app")

    with pytest.warns(DeprecationWarning, match="get_persistent_s3_prefix"):
        prefix = state_reader.get_persistent_s3_prefix("default/x/1", "app")
    assert prefix == helpers.get_persistent_s3_prefix("default/x/1", "app")

    (tmp_path / "a.json").write_text("{}")
    with pytest.warns(DeprecationWarning, match="count_json_files_recursive"):
        assert state_reader.count_json_files_recursive(tmp_path) == 1

    target = tmp_path / "out.bin"
    with pytest.warns(DeprecationWarning, match="atomic_write"):
        with marker.atomic_write(target, operation="test") as fh:
            fh.write(b"x")
    assert target.read_bytes() == b"x"


async def test_async_function_aliases_warn_on_call() -> None:
    with pytest.warns(DeprecationWarning, match="run_in_thread"):
        assert await state_reader.run_in_thread(lambda: 7) == 7


@pytest.mark.parametrize(
    ("module", "name", "real"),
    [
        (helpers, "StorageNotFoundError", errors.StorageNotFoundError),
        (state_reader, "StorageError", errors.StorageError),
    ],
)
def test_class_aliases_are_the_real_class(module, name, real) -> None:
    with pytest.warns(DeprecationWarning, match=name):
        alias = getattr(module, name)
    assert alias is real


def test_aliases_are_distinct_from_their_targets() -> None:
    """A wrapper, not the target re-bound: rebinding would silence the warning."""
    assert marker.atomic_write is not atomic.atomic_write
    assert state_reader.run_in_thread is not offload.run_in_thread

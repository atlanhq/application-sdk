"""Unit tests for :mod:`tests.unit.private_stdlib`."""

from __future__ import annotations

import time

import pytest

from application_sdk.observability import pushgateway
from tests.unit.private_stdlib import private_stdlib


def test_a_patch_on_the_copy_reaches_only_the_owner() -> None:
    real_monotonic = time.monotonic
    with private_stdlib(pushgateway, "time") as local_time:
        local_time.monotonic = lambda: 42.0
        assert pushgateway.time.monotonic() == 42.0
        assert time.monotonic is real_monotonic
    assert pushgateway.time is time


def test_a_dotted_owner_path_resolves_to_the_module() -> None:
    with private_stdlib("application_sdk.observability.pushgateway", "time") as copy:
        assert pushgateway.time is copy
        assert copy is not time
    assert pushgateway.time is time


def test_a_name_that_is_not_a_module_raises_type_error() -> None:
    with pytest.raises(TypeError, match="is not a module"):
        with private_stdlib(pushgateway, "__name__"):
            pass
    assert pushgateway.__name__ == "application_sdk.observability.pushgateway"

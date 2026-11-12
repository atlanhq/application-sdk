"""Tests for ``needs_lock``: the metadata it writes is what the lock interceptor reads.

``test_lock_interceptor.py`` hand-builds the metadata dict, so it cannot notice
the decorator and the interceptor drifting apart. These tests decorate a real
function and hand it to the real interceptor.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.constants import _LOCK_METADATA_KEY as LOCK_METADATA_KEY
from application_sdk.execution import needs_lock
from application_sdk.execution._temporal.interceptors.lock import (
    RedisLockOutboundInterceptor,
)

_LOCK_MODULE = "application_sdk.execution._temporal.interceptors.lock"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, {"is_needs_lock": True, "max_locks": 5, "lock_name": "fetch_tables"}),
        (
            {"max_locks": 2},
            {"is_needs_lock": True, "max_locks": 2, "lock_name": "fetch_tables"},
        ),
        (
            {"lock_name": "source-db"},
            {"is_needs_lock": True, "max_locks": 5, "lock_name": "source-db"},
        ),
        (
            {"max_locks": 1, "lock_name": "source-db"},
            {"is_needs_lock": True, "max_locks": 1, "lock_name": "source-db"},
        ),
    ],
)
def test_metadata(kwargs: dict, expected: dict) -> None:
    async def fetch_tables() -> str:
        return "fetched"

    decorated = needs_lock(**kwargs)(fetch_tables)

    assert getattr(decorated, LOCK_METADATA_KEY) == expected


async def test_returns_the_function_itself() -> None:
    """No wrapper: Temporal registers the activity by its own name and signature."""

    async def fetch_tables() -> str:
        return "fetched"

    decorated = needs_lock()(fetch_tables)

    assert decorated is fetch_tables
    assert await decorated() == "fetched"


def test_each_function_gets_its_own_metadata() -> None:
    def first() -> None: ...

    def second() -> None: ...

    decorator = needs_lock(max_locks=3)
    decorator(first)
    decorator(second)

    assert getattr(first, LOCK_METADATA_KEY)["lock_name"] == "first"
    assert getattr(second, LOCK_METADATA_KEY)["lock_name"] == "second"


@pytest.mark.parametrize(
    ("kwargs", "lock_name", "max_locks"),
    [
        ({}, "fetch_tables", 5),
        ({"max_locks": 2, "lock_name": "source-db"}, "source-db", 2),
    ],
)
async def test_interceptor_acquires_the_lock_the_decorator_declared(
    kwargs: dict, lock_name: str, max_locks: int
) -> None:
    async def fetch_tables() -> str:
        return "fetched"

    activity_fn = needs_lock(**kwargs)(fetch_tables)
    next_interceptor = MagicMock()

    async def _handle() -> str:
        return "fetched"

    next_interceptor.start_activity = AsyncMock(side_effect=lambda _: _handle())
    interceptor = RedisLockOutboundInterceptor(
        next_interceptor, {"fetch_tables": activity_fn}
    )
    start_input = MagicMock()
    start_input.activity = "fetch_tables"
    start_input.schedule_to_close_timeout = timedelta(minutes=10)

    wf_info = MagicMock(run_id="run-1", execution_timeout=timedelta(hours=1))
    execute_activity = AsyncMock(
        return_value={"resource_id": "lock-1", "owner_id": "app:run-1"}
    )
    with (
        patch(f"{_LOCK_MODULE}.IS_LOCKING_DISABLED", False),
        patch(f"{_LOCK_MODULE}.workflow.execute_activity", execute_activity),
        patch(f"{_LOCK_MODULE}.workflow.info", return_value=wf_info),
    ):
        await interceptor.start_activity(start_input)

    acquire = execute_activity.await_args_list[0]
    assert acquire.args == ("acquire_distributed_lock",)
    assert acquire.kwargs["args"][:3] == [lock_name, max_locks, 600]


def test_needs_lock_warns_that_it_has_no_effect() -> None:
    """FND-4888: create_worker wires no lock interceptor, so the decorator says so."""

    async def fetch_tables() -> None:
        return None

    with pytest.warns(DeprecationWarning, match=r"has no effect.*v4\.0\.0"):
        needs_lock()(fetch_tables)


@pytest.mark.parametrize("name", ["LOCK_METADATA_KEY", "LOCK_RETRY_INTERVAL_SECONDS"])
def test_lock_constants_are_deprecated_aliases(name: str) -> None:
    import application_sdk.constants as constants

    with pytest.warns(DeprecationWarning, match=rf"constants\.{name} is deprecated"):
        value = getattr(constants, name)
    assert value == getattr(constants, f"_{name}")

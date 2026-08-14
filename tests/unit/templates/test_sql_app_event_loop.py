"""FND-282: the SqlApp extract/transform bodies must not starve the event loop.

``SqlApp`` is the base class every SQL connector inherits, so a blocking loop
in ``_extract_entity`` / ``_transform_entity`` is a fleet-wide starvation site:
the auto-heartbeat coroutine cannot run, and Temporal kills an activity that
was making progress throughout (ADR-0010, ADR-0018 *Problem 2*).

``_transform_entity`` is the sharper case — it maps every raw record for the
entity (JSON parse, the connector's ``map_*`` function, serialise, write) with
no await anywhere in the loop, so its hold scales with the source table.

Both tests drive the real method while a ticker coroutine counts how often the
loop got to run something else, so they assert the property that matters rather
than which callable was handed to ``run_in_thread``.

The tick count on its own measures how much CPU the loop thread got, not
whether the body offloaded: a *correctly* offloaded block scores 27 ticks on an
idle machine and 4 with a handful of unrelated GIL-holding threads in the
process, with wall-clock elapsed unchanged either way. An absolute threshold
therefore fails on a loaded shared runner with nothing wrong (FND-360). Each
test instead measures a known-good offload of the same length on the same
runner, moments away, and requires the subject to reach ``MIN_TICK_RATIO`` of
it — runner noise moves both numbers together, while a body that blocks the
loop scores ~0 against whatever the control scored.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import orjson
import pytest

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.templates import sql_app as sql_app_module
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    TransformInput,
)
from application_sdk.templates.sql_app import SqlApp

pytestmark = pytest.mark.asyncio

T = TypeVar("T")

BLOCK_SECONDS = 0.3
TICK_SECONDS = 0.01

#: Share of the control's ticks a non-blocking body must reach. Half leaves room
#: for the runner's load to drift between the two measurements while staying far
#: above what a body that holds the loop can score.
MIN_TICK_RATIO = 0.5

#: Below this the control itself could not schedule enough ticks to tell a
#: healthy body from a blocking one, so the run says so rather than passing
#: vacuously or failing on the runner's behalf.
MIN_CONTROL_TICKS = 4


class _FakeSqlClient(BaseSQLClient):
    """In-process SQL client yielding one fixed batch — no network, no driver."""

    _ROWS: ClassVar[list[dict[str, Any]]] = [
        {"database_name": "prod"},
        {"database_name": "stage"},
    ]

    def __init__(self) -> None:
        pass  # skip BaseSQLClient.__init__ — no DB_CONFIG needed for the fake

    async def load(self, credentials: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def run_query(self, query: str, batch_size: int = 100_000):
        yield self._ROWS


class _SlowMapperApp(SqlApp):
    """SqlApp whose mapper is expensive, standing in for a real pyatlan mapper."""

    sql_client_class: ClassVar[type[BaseSQLClient] | None] = _FakeSqlClient
    _app_registered: ClassVar[bool] = True

    fetch_database_sql: ClassVar[str] = "SELECT 1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mapped = 0

    def map_database(
        self, record: dict[str, Any], connection_qn: str
    ) -> dict[str, Any]:
        # Charge the cost once — a real mapper's per-record cost times a large
        # table is what produces the multi-second holds seen in production.
        if self.mapped == 0:
            time.sleep(BLOCK_SECONDS)
        self.mapped += 1
        return {
            "typeName": "Database",
            "attributes": {
                "name": record["database_name"],
                "qualifiedName": f"{connection_qn}/{record['database_name']}",
            },
        }


async def count_ticks_during(work: Callable[[], Awaitable[T]]) -> tuple[T, int]:
    """Run *work*, returning its result and how many times the loop ticked."""
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_SECONDS)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await work()
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
    return result, ticks


async def control_ticks() -> int:
    """Ticks a correctly-offloaded block of the same length gets on this runner.

    Deliberately stdlib — ``asyncio.to_thread``, never the SDK's
    ``run_in_thread`` — so that a regression *inside* the SDK's own offload path
    collapses the subject's ticks without also collapsing the baseline it is
    measured against.
    """
    _, ticks = await count_ticks_during(
        lambda: asyncio.to_thread(time.sleep, BLOCK_SECONDS)
    )
    return ticks


def assert_loop_stayed_live(*, method: str, ticks: int, control: int) -> None:
    """Fail if *method* left the loop less responsive than a known-good offload."""
    if control < MIN_CONTROL_TICKS:
        pytest.skip(
            f"runner too starved to measure: a stdlib offload of "
            f"{BLOCK_SECONDS}s scored only {control} ticks, so no threshold "
            "distinguishes a healthy body from a blocking one"
        )

    floor = control * MIN_TICK_RATIO
    assert ticks >= floor, (
        f"{method} stalled the event loop: {ticks} ticks against {control} for "
        f"an offload of the same length on this runner (floor {floor:.1f}) — a "
        "starved loop cannot run the auto-heartbeat, which is what gets healthy "
        "activities killed on heartbeat_timeout"
    )


def _extract_input(tmp_path: Path) -> ExtractionTaskInput:
    return ExtractionTaskInput(
        workflow_id="wf-test",
        output_path=str(tmp_path),
        output_prefix=str(tmp_path),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
    )


def _transform_input(tmp_path: Path) -> TransformInput:
    return TransformInput(
        workflow_id="wf-test",
        output_path=str(tmp_path),
        output_prefix=str(tmp_path),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
        raw_file=None,
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> _SlowMapperApp:
    instance = _SlowMapperApp()

    async def _fake_init(self: Any, _input: Any) -> BaseSQLClient:
        return _FakeSqlClient()

    monkeypatch.setattr(SqlApp, "_init_sql_client", _fake_init)
    return instance


async def test_transform_entity_does_not_block_the_loop(
    app: _SlowMapperApp, tmp_path: Path
) -> None:
    raw_dir = tmp_path / "raw" / "database"
    raw_dir.mkdir(parents=True)
    with (raw_dir / "records.json").open("wb") as f:
        for name in ("prod", "stage"):
            f.write(orjson.dumps({"database_name": name}))
            f.write(b"\n")

    control = await control_ticks()
    result, ticks = await count_ticks_during(
        lambda: app._transform_entity(
            entity_type="database",
            mapper_fn=app.map_database,
            input=_transform_input(tmp_path),
        )
    )

    assert result.total_record_count == 2
    assert app.mapped == 2, "the mapper never ran"
    assert_loop_stayed_live(method="_transform_entity", ticks=ticks, control=control)


async def test_extract_entity_does_not_block_the_loop(
    app: _SlowMapperApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dumps = orjson.dumps
    slowed = {"done": False}

    def _slow_dumps(*args: Any, **kwargs: Any) -> bytes:
        # Charge the cost once, standing in for a full 10k-row batch.
        if not slowed["done"]:
            slowed["done"] = True
            time.sleep(BLOCK_SECONDS)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr("application_sdk.templates.sql_app.orjson.dumps", _slow_dumps)

    # Measured after the patch and immediately before the subject: the control
    # never serialises, so it cannot spend the one-shot cost above, and the two
    # measurements sit close enough together to see the same runner load.
    control = await control_ticks()
    result, ticks = await count_ticks_during(
        lambda: app._extract_entity(
            entity_type="database",
            sql_template=_SlowMapperApp.fetch_database_sql,
            input=_extract_input(tmp_path),
        )
    )

    assert result.total_record_count == 2
    assert slowed["done"], "the serialise path never ran"
    assert_loop_stayed_live(method="_extract_entity", ticks=ticks, control=control)


async def test_extract_entity_never_hands_the_file_handle_to_a_thread(
    app: _SlowMapperApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only serialisation may be offloaded — never the open output handle.

    If the handle crossed into the blocking pool, a cancellation at that await
    would unwind the enclosing ``with`` and close the file while a worker thread
    was still writing to it. Per ADR-0010 that thread cannot be killed, so it
    would write on into a closed — or retry-reopened — file.

    Asserts the invariant directly: nothing passed to ``run_in_thread`` is a
    file object, and the bytes still land on disk.
    """
    offloaded_args: list[Any] = []
    real_run_in_thread = sql_app_module.run_in_thread

    async def _recording_run_in_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded_args.extend(args)
        offloaded_args.extend(kwargs.values())
        return await real_run_in_thread(func, *args, **kwargs)

    monkeypatch.setattr(sql_app_module, "run_in_thread", _recording_run_in_thread)

    result = await app._extract_entity(
        entity_type="database",
        sql_template=_SlowMapperApp.fetch_database_sql,
        input=_extract_input(tmp_path),
    )

    assert offloaded_args, "nothing was offloaded"
    for arg in offloaded_args:
        assert not hasattr(arg, "write"), (
            f"a file-like object ({arg!r}) was handed to run_in_thread — a "
            "cancellation there would close the handle under a live writer"
        )

    assert result.total_record_count == 2
    written = (tmp_path / "raw" / "database" / "records.json").read_bytes()
    assert written.count(b"\n") == 2

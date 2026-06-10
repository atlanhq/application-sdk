"""Tests for App.map_batched — chunked task fan-out helper.

map_batched is a plain (deterministic) instance method, so it is exercised
directly with stub async callables — no Temporal machinery required. The
task boundary itself (activity execution) is covered by the
_create_task_activity_wrapper tests in test_base.py.
"""

import asyncio
from typing import Annotated

import pytest

from application_sdk.app.base import App
from application_sdk.app.base_errors import MapBatchedInvalidArgumentError
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems

# =============================================================================
# Test fixtures
# =============================================================================


class ChunkInput(Input):
    items: Annotated[list[int], MaxItems(1000)] = []


class ChunkOutput(Output):
    values: Annotated[list[int], MaxItems(1000)] = []


class _BatchApp(App):
    async def run(self, input: ChunkInput) -> ChunkOutput:  # pragma: no cover
        return ChunkOutput()


@pytest.fixture
def app() -> _BatchApp:
    return _BatchApp()


def _make_input(chunk: list[int]) -> ChunkInput:
    return ChunkInput(items=chunk)


# =============================================================================
# Chunking and ordering
# =============================================================================


class TestMapBatchedChunking:
    async def test_empty_items_returns_empty_without_invoking_task(
        self, app: _BatchApp
    ) -> None:
        calls: list[list[int]] = []

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            calls.append(input.items)
            return ChunkOutput(values=input.items)

        result = await app.map_batched(
            fake_task, [], batch_size=10, input_factory=_make_input
        )
        assert result == []
        assert calls == []

    async def test_batch_size_one_invokes_task_per_item(self, app: _BatchApp) -> None:
        calls: list[list[int]] = []

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            calls.append(input.items)
            return ChunkOutput(values=input.items)

        outputs = await app.map_batched(
            fake_task, [1, 2, 3], batch_size=1, input_factory=_make_input
        )
        assert calls == [[1], [2], [3]]
        assert [o.values for o in outputs] == [[1], [2], [3]]

    async def test_uneven_final_chunk(self, app: _BatchApp) -> None:
        """len(items) % batch_size != 0 → last chunk is smaller."""
        calls: list[list[int]] = []

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            calls.append(input.items)
            return ChunkOutput(values=input.items)

        await app.map_batched(
            fake_task, list(range(7)), batch_size=3, input_factory=_make_input
        )
        assert calls == [[0, 1, 2], [3, 4, 5], [6]]

    async def test_results_preserve_chunk_order_despite_completion_order(
        self, app: _BatchApp
    ) -> None:
        """Earlier chunks that finish later still come back first."""

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            # First chunk sleeps longest → completes last.
            await asyncio.sleep(0.05 if input.items[0] == 0 else 0)
            return ChunkOutput(values=[v * 10 for v in input.items])

        outputs = await app.map_batched(
            fake_task, [0, 1, 2, 3], batch_size=2, input_factory=_make_input
        )
        assert [o.values for o in outputs] == [[0, 10], [20, 30]]

    async def test_flatten_idiom(self, app: _BatchApp) -> None:
        """The documented flatten comprehension yields per-item results in order."""

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            return ChunkOutput(values=[v + 100 for v in input.items])

        outputs = await app.map_batched(
            fake_task, [1, 2, 3, 4, 5], batch_size=2, input_factory=_make_input
        )
        flat = [v for out in outputs for v in out.values]
        assert flat == [101, 102, 103, 104, 105]


# =============================================================================
# Concurrency
# =============================================================================


class TestMapBatchedConcurrency:
    async def test_max_concurrency_bounds_in_flight_chunks(
        self, app: _BatchApp
    ) -> None:
        in_flight = 0
        peak = 0

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return ChunkOutput(values=input.items)

        await app.map_batched(
            fake_task,
            list(range(12)),
            batch_size=1,
            max_concurrency=3,
            input_factory=_make_input,
        )
        assert peak <= 3

    async def test_chunks_run_concurrently_up_to_limit(self, app: _BatchApp) -> None:
        in_flight = 0
        peak = 0

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return ChunkOutput(values=input.items)

        await app.map_batched(
            fake_task,
            list(range(6)),
            batch_size=1,
            max_concurrency=4,
            input_factory=_make_input,
        )
        assert peak > 1  # genuinely concurrent, not sequential


# =============================================================================
# Error propagation
# =============================================================================


class TestMapBatchedErrors:
    async def test_error_in_one_chunk_propagates(self, app: _BatchApp) -> None:
        """A failing chunk fails the whole call (after its own task retries)."""

        class ChunkBoom(RuntimeError):
            pass

        async def fake_task(input: ChunkInput) -> ChunkOutput:
            if 3 in input.items:
                raise ChunkBoom("chunk failed")
            return ChunkOutput(values=input.items)

        with pytest.raises(ChunkBoom, match="chunk failed"):
            await app.map_batched(
                fake_task, [1, 2, 3, 4], batch_size=2, input_factory=_make_input
            )


# =============================================================================
# Argument validation (ADR-0013 typed errors)
# =============================================================================


class TestMapBatchedValidation:
    async def _noop_task(self, input: ChunkInput) -> ChunkOutput:  # pragma: no cover
        return ChunkOutput()

    async def test_batch_size_zero_raises_typed_error(self, app: _BatchApp) -> None:
        with pytest.raises(
            MapBatchedInvalidArgumentError, match=r"batch_size must be >= 1"
        ) as exc_info:
            await app.map_batched(
                self._noop_task, [1], batch_size=0, input_factory=_make_input
            )
        assert exc_info.value.field == "batch_size"

    async def test_negative_batch_size_raises_typed_error(self, app: _BatchApp) -> None:
        with pytest.raises(
            MapBatchedInvalidArgumentError, match=r"batch_size must be >= 1"
        ):
            await app.map_batched(
                self._noop_task, [1], batch_size=-5, input_factory=_make_input
            )

    async def test_max_concurrency_zero_raises_typed_error(
        self, app: _BatchApp
    ) -> None:
        with pytest.raises(
            MapBatchedInvalidArgumentError, match=r"max_concurrency must be >= 1"
        ) as exc_info:
            await app.map_batched(
                self._noop_task,
                [1],
                batch_size=1,
                max_concurrency=0,
                input_factory=_make_input,
            )
        assert exc_info.value.field == "max_concurrency"

    def test_error_is_invalid_input_leaf(self) -> None:
        """MapBatchedInvalidArgumentError is a typed InvalidInputError (ADR-0013)."""
        from application_sdk.errors.leaves import InvalidInputError

        assert issubclass(MapBatchedInvalidArgumentError, InvalidInputError)
        assert (
            MapBatchedInvalidArgumentError.code == "INVALID_INPUT_MAP_BATCHED_ARGUMENT"
        )

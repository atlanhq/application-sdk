import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.events_read import events_read
from application_sdk.lakehouse.models import EventResult


def _make_handler(results=None, raise_in_handler=False, raise_after_n_calls=None):
    """Build an async handler for tests, recording invocations.

    ``raise_after_n_calls`` raises only on the Nth+ call (1-indexed), useful
    for tests that drive the loop a few iterations before failing.
    """
    state = {"calls": [], "raise": raise_in_handler}

    async def fn(events):
        state["calls"].append(events)
        if (
            raise_after_n_calls is not None
            and len(state["calls"]) >= raise_after_n_calls
        ):
            raise RuntimeError("boom")
        if state["raise"]:
            raise RuntimeError("boom")
        if results is not None:
            return results
        return [EventResult(status="SUCCESS") for _ in events]

    fn.calls = state["calls"]  # type: ignore[attr-defined]
    return fn


def _scripted_reader(batches):
    """Patch LakehouseReader.from_env to return a reader that yields ``batches``
    one fetch_records() call at a time, then ``[]`` forever after.

    Respects the ``limit`` kwarg by truncating the next batch to it — matches
    real reader behaviour so cap-and-loop logic exercises correctly.
    """
    fake_reader = MagicMock()
    iterator = iter(batches)

    def _fetch(*_args, **kwargs):
        try:
            batch = next(iterator)
        except StopIteration:
            return []
        limit = kwargs.get("limit")
        return batch[:limit] if limit is not None else batch

    fake_reader.fetch_records = MagicMock(side_effect=_fetch)
    return patch(
        "application_sdk.lakehouse.events_read.LakehouseReader.from_env",
        return_value=fake_reader,
    ), fake_reader


def _flat_reader(events):
    """Single-fetch reader: returns ``events`` once, then ``[]``."""
    return _scripted_reader([events])


class TestEventsReadSingleFetch(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_pair_and_skips_handler(self):
        handler = _make_handler()
        ctx, _ = _flat_reader([])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        self.assertEqual(handler.calls, [])

    async def test_dispatches_events_to_handler(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        handler = _make_handler()
        ctx, _ = _flat_reader(events)
        with ctx:
            out_events, results = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(out_events, events)
        self.assertEqual([r.status for r in results], ["SUCCESS", "SUCCESS"])
        self.assertEqual(handler.calls, [events])

    async def test_handler_exception_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        handler = _make_handler(raise_in_handler=True)
        ctx, _ = _flat_reader(events)
        with ctx:
            _, results = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("batch processing failed" in (r.error_message or "") for r in results)
        )

    async def test_result_count_mismatch_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e3"}]
        handler = _make_handler(results=[EventResult(status="SUCCESS")])
        ctx, _ = _flat_reader(events)
        with ctx:
            _, results = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("count mismatch" in (r.error_message or "") for r in results)
        )

    async def test_passes_filter_and_sort_to_reader(self):
        events = [{"event_id": "e1"}]
        ctx, fake_reader = _flat_reader(events)
        with ctx:
            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                where="status = 'pending'",
                sort_by="ingested_at",
            )
        fake_reader.fetch_records.assert_called_once_with(
            "automation_engine",
            "events",
            where="status = 'pending'",
            sort_by="ingested_at",
            limit=None,
        )

    async def test_max_events_only_caps_single_fetch(self):
        events = [{"event_id": f"e{i}"} for i in range(3)]
        ctx, fake_reader = _flat_reader(events)
        with ctx:
            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                max_events=10,
            )
        # Single call with limit=max_events
        fake_reader.fetch_records.assert_called_once()
        self.assertEqual(fake_reader.fetch_records.call_args.kwargs["limit"], 10)

    async def test_reader_built_from_env_each_call(self):
        with patch(
            "application_sdk.lakehouse.events_read.LakehouseReader.from_env"
        ) as from_env:
            fake_reader = MagicMock()
            fake_reader.fetch_records = MagicMock(return_value=[])
            from_env.return_value = fake_reader

            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
            )
            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
            )
            self.assertEqual(from_env.call_count, 2)


class TestEventsReadBatching(unittest.IsolatedAsyncioTestCase):
    async def test_batch_size_loops_until_exhausted(self):
        b1 = [{"event_id": f"e{i}"} for i in range(1000)]
        b2 = [
            {"event_id": f"e{i}"} for i in range(1000, 1500)
        ]  # short batch — exhausted
        handler = _make_handler()
        ctx, fake_reader = _scripted_reader([b1, b2])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=1000,
            )
        self.assertEqual(len(events), 1500)
        self.assertEqual(len(results), 1500)
        # Two handler calls, one per batch
        self.assertEqual(len(handler.calls), 2)
        self.assertEqual(len(handler.calls[0]), 1000)
        self.assertEqual(len(handler.calls[1]), 500)
        # Reader called twice with limit=1000 each time
        self.assertEqual(fake_reader.fetch_records.call_count, 2)
        for call in fake_reader.fetch_records.call_args_list:
            self.assertEqual(call.kwargs["limit"], 1000)

    async def test_batch_size_with_max_events_caps_total(self):
        b1 = [{"event_id": f"e{i}"} for i in range(1000)]
        b2 = [{"event_id": f"e{i}"} for i in range(1000, 2000)]
        b3 = [{"event_id": f"e{i}"} for i in range(2000, 3000)]
        handler = _make_handler()
        ctx, fake_reader = _scripted_reader([b1, b2, b3])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=1000,
                max_events=2500,
            )
        self.assertEqual(len(events), 2500)
        self.assertEqual(len(results), 2500)
        # Three reader calls: 1000, 1000, 500 (last shrunk to fit max_events)
        limits = [c.kwargs["limit"] for c in fake_reader.fetch_records.call_args_list]
        self.assertEqual(limits, [1000, 1000, 500])

    async def test_handler_exception_aborts_loop_after_current_batch(self):
        b1 = [{"event_id": f"e{i}"} for i in range(10)]
        b2 = [{"event_id": f"e{i}"} for i in range(10, 20)]
        b3 = [{"event_id": f"e{i}"} for i in range(20, 30)]
        # Raise on the 2nd call (during batch 2)
        handler = _make_handler(raise_after_n_calls=2)
        ctx, fake_reader = _scripted_reader([b1, b2, b3])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=10,
                max_events=100,
            )
        # Got batch 1 (SUCCESS) + batch 2 (RETRY) — batch 3 never read
        self.assertEqual(len(events), 20)
        self.assertEqual(len(results), 20)
        self.assertEqual(set(r.status for r in results[:10]), {"SUCCESS"})
        self.assertEqual(set(r.status for r in results[10:]), {"RETRY"})
        # Reader called twice — third batch not requested
        self.assertEqual(fake_reader.fetch_records.call_count, 2)

    async def test_count_mismatch_aborts_loop_after_current_batch(self):
        b1 = [{"event_id": f"e{i}"} for i in range(10)]
        b2 = [{"event_id": f"e{i}"} for i in range(10, 20)]

        # First call returns matching results, second returns wrong count.
        handler_calls = {"n": 0}

        async def handler(events):
            handler_calls["n"] += 1
            if handler_calls["n"] == 1:
                return [EventResult(status="SUCCESS") for _ in events]
            return [EventResult(status="SUCCESS")]  # count mismatch

        ctx, fake_reader = _scripted_reader([b1, b2])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=10,
                max_events=100,
            )
        self.assertEqual(len(events), 20)
        self.assertEqual(len(results), 20)
        self.assertEqual(set(r.status for r in results[:10]), {"SUCCESS"})
        self.assertEqual(set(r.status for r in results[10:]), {"RETRY"})

    async def test_empty_first_batch_returns_empty_pair(self):
        ctx, fake_reader = _scripted_reader([[]])
        with ctx:
            events, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                batch_size=1000,
                max_events=5000,
            )
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        self.assertEqual(fake_reader.fetch_records.call_count, 1)

    async def test_accepts_bound_method_as_handler(self):
        """Class-style users can still pass instance.method as handler."""

        class Worker:
            def __init__(self):
                self.received = []

            async def process_batch(self, events):
                self.received.append(events)
                return [EventResult(status="SUCCESS") for _ in events]

        w = Worker()
        events = [{"event_id": "e1"}]
        ctx, _ = _flat_reader(events)
        with ctx:
            _, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=w.process_batch,
            )
        self.assertEqual([r.status for r in results], ["SUCCESS"])
        self.assertEqual(w.received, [events])


if __name__ == "__main__":
    unittest.main()

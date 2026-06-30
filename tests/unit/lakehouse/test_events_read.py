import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.events_read import events_read
from application_sdk.lakehouse.models import EventResult


def _make_handler(results=None, raise_in_handler=False, raise_after_n_calls=None):
    """Build an async handler for tests, recording invocations.

    ``raise_after_n_calls`` raises only on the Nth+ call (1-indexed), useful
    for tests that drive a few chunks before failing.
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


def _stateless_reader(rows):
    """Patch LakehouseReader.from_env with a reader that has NO cursor — it
    re-applies ``limit`` to a FIXED row set on every call, exactly like the
    real Iceberg reader. Crucially it does NOT advance: calling fetch twice
    with the same limit returns the same head rows (this is what the real
    reader does and what the old iterator-based mock failed to model).
    """
    fake_reader = MagicMock()

    def _fetch(*_args, limit=None, **_kwargs):
        return list(rows[:limit]) if limit is not None else list(rows)

    fake_reader.fetch_records = MagicMock(side_effect=_fetch)
    return patch(
        "application_sdk.lakehouse.events_read.LakehouseReader.from_env",
        return_value=fake_reader,
    ), fake_reader


class TestEventsReadSingleFetch(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_and_skips_handler(self):
        handler = _make_handler()
        ctx, _ = _stateless_reader([])
        with ctx:
            result = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(result.events, [])
        self.assertEqual(result.results, [])
        self.assertTrue(result.complete)
        self.assertEqual(handler.calls, [])

    async def test_dispatches_events_to_handler(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        handler = _make_handler()
        ctx, _ = _stateless_reader(events)
        with ctx:
            result = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(result.events, events)
        self.assertEqual([r.status for r in result.results], ["SUCCESS", "SUCCESS"])
        self.assertTrue(result.complete)
        # batch_size None → one handler call with everything
        self.assertEqual(handler.calls, [events])

    async def test_handler_exception_marks_all_retry_and_incomplete(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        handler = _make_handler(raise_in_handler=True)
        ctx, _ = _stateless_reader(events)
        with ctx:
            result = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(len(result.results), 2)
        self.assertTrue(all(r.status == "RETRY" for r in result.results))
        self.assertFalse(result.complete)
        self.assertTrue(
            all(
                "batch processing failed" in (r.error_message or "")
                for r in result.results
            )
        )

    async def test_result_count_mismatch_marks_all_retry_and_incomplete(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e3"}]
        handler = _make_handler(results=[EventResult(status="SUCCESS")])
        ctx, _ = _stateless_reader(events)
        with ctx:
            result = await events_read(
                namespace="automation_engine", table="events", handler=handler
            )
        self.assertEqual(len(result.results), 3)
        self.assertTrue(all(r.status == "RETRY" for r in result.results))
        self.assertFalse(result.complete)
        self.assertTrue(
            all("count mismatch" in (r.error_message or "") for r in result.results)
        )

    async def test_passes_filter_and_sort_to_reader_once(self):
        ctx, fake_reader = _stateless_reader([{"event_id": "e1"}])
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

    async def test_max_events_caps_the_scan(self):
        events = [{"event_id": f"e{i}"} for i in range(3)]
        ctx, fake_reader = _stateless_reader(events)
        with ctx:
            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                max_events=10,
            )
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
                namespace="automation_engine", table="events", handler=_make_handler()
            )
            await events_read(
                namespace="automation_engine", table="events", handler=_make_handler()
            )
            self.assertEqual(from_env.call_count, 2)


class TestEventsReadChunking(unittest.IsolatedAsyncioTestCase):
    async def test_batch_size_chunks_a_single_scan(self):
        rows = [{"event_id": f"e{i}"} for i in range(1500)]
        handler = _make_handler()
        ctx, fake_reader = _stateless_reader(rows)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=1000,
            )
        self.assertEqual(len(result.events), 1500)
        self.assertEqual(len(result.results), 1500)
        self.assertTrue(result.complete)
        # ONE scan, then dispatched in chunks of 1000 / 500
        self.assertEqual(fake_reader.fetch_records.call_count, 1)
        self.assertEqual([len(c) for c in handler.calls], [1000, 500])

    async def test_no_duplicate_event_ids_across_chunks(self):
        # Regression for the batch-loop bug: against a stateless reader the old
        # loop re-read the same head rows every iteration, returning duplicates
        # and never advancing. A single scan + in-memory chunking must not.
        rows = [{"event_id": f"e{i}"} for i in range(2500)]
        handler = _make_handler()
        ctx, fake_reader = _stateless_reader(rows)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=1000,
                max_events=2500,
            )
        ids = [e["event_id"] for e in result.events]
        self.assertEqual(len(ids), 2500)
        self.assertEqual(len(set(ids)), 2500)  # no duplicates
        self.assertEqual(fake_reader.fetch_records.call_count, 1)  # single scan
        self.assertTrue(result.complete)

    async def test_max_events_caps_then_chunks(self):
        rows = [{"event_id": f"e{i}"} for i in range(3000)]
        handler = _make_handler()
        ctx, fake_reader = _stateless_reader(rows)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=1000,
                max_events=2500,
            )
        self.assertEqual(len(result.events), 2500)
        self.assertTrue(result.complete)
        # single scan capped at max_events, then chunked 1000/1000/500
        self.assertEqual(fake_reader.fetch_records.call_count, 1)
        self.assertEqual(fake_reader.fetch_records.call_args.kwargs["limit"], 2500)
        self.assertEqual([len(c) for c in handler.calls], [1000, 1000, 500])

    async def test_handler_exception_marks_failed_chunk_and_tail_retry(self):
        rows = [{"event_id": f"e{i}"} for i in range(30)]
        handler = _make_handler(raise_after_n_calls=2)  # fail on the 2nd chunk
        ctx, fake_reader = _stateless_reader(rows)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=10,
                max_events=100,
            )
        # All 30 fetched events are accounted for: chunk 1 SUCCESS, the failed
        # chunk + untouched tail (20) RETRY. complete=False signals the abort.
        self.assertEqual(len(result.events), 30)
        self.assertEqual(len(result.results), 30)
        self.assertEqual({r.status for r in result.results[:10]}, {"SUCCESS"})
        self.assertEqual({r.status for r in result.results[10:]}, {"RETRY"})
        self.assertFalse(result.complete)
        self.assertEqual(fake_reader.fetch_records.call_count, 1)

    async def test_count_mismatch_marks_failed_chunk_and_tail_retry(self):
        rows = [{"event_id": f"e{i}"} for i in range(20)]
        calls = {"n": 0}

        async def handler(events):
            calls["n"] += 1
            if calls["n"] == 1:
                return [EventResult(status="SUCCESS") for _ in events]
            return [EventResult(status="SUCCESS")]  # wrong count on chunk 2

        ctx, _ = _stateless_reader(rows)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=handler,
                batch_size=10,
                max_events=100,
            )
        self.assertEqual(len(result.events), 20)
        self.assertEqual({r.status for r in result.results[:10]}, {"SUCCESS"})
        self.assertEqual({r.status for r in result.results[10:]}, {"RETRY"})
        self.assertFalse(result.complete)

    async def test_empty_scan_returns_empty_complete(self):
        ctx, fake_reader = _stateless_reader([])
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                batch_size=1000,
                max_events=5000,
            )
        self.assertEqual(result.events, [])
        self.assertEqual(result.results, [])
        self.assertTrue(result.complete)
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
        ctx, _ = _stateless_reader(events)
        with ctx:
            result = await events_read(
                namespace="automation_engine",
                table="events",
                handler=w.process_batch,
            )
        self.assertEqual([r.status for r in result.results], ["SUCCESS"])
        self.assertTrue(result.complete)
        self.assertEqual(w.received, [events])


if __name__ == "__main__":
    unittest.main()

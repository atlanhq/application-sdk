import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.events_consumer import EventsConsumer
from application_sdk.lakehouse.models import ProcessingResult


def _make_process_fn(results=None, raise_in_process=False):
    """Build an async process_fn for tests, recording invocations."""
    state = {"calls": [], "raise": raise_in_process}

    async def fn(events):
        state["calls"].append(events)
        if state["raise"]:
            raise RuntimeError("boom")
        if results is not None:
            return results
        return [ProcessingResult(status="SUCCESS") for _ in events]

    fn.calls = state["calls"]  # type: ignore[attr-defined]
    return fn


def _consumer_with_events(events, process_fn=None):
    """Build an EventsConsumer whose internal reader returns ``events``."""
    consumer = EventsConsumer(process_fn or _make_process_fn())
    fake_reader = MagicMock()
    fake_reader.fetch_records = MagicMock(return_value=events)
    consumer._reader = fake_reader
    return consumer


class TestEventsConsumer(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_pair_and_skips_process_fn(self):
        process_fn = _make_process_fn()
        consumer = _consumer_with_events([], process_fn=process_fn)
        events, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        self.assertEqual(process_fn.calls, [])

    async def test_dispatches_events_to_process_fn(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        process_fn = _make_process_fn()
        consumer = _consumer_with_events(events, process_fn=process_fn)
        out_events, results = await consumer.handle_events(
            "automation_engine", "events"
        )
        self.assertEqual(out_events, events)
        self.assertEqual([r.status for r in results], ["SUCCESS", "SUCCESS"])
        self.assertEqual(process_fn.calls, [events])

    async def test_process_fn_exception_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        consumer = _consumer_with_events(
            events, process_fn=_make_process_fn(raise_in_process=True)
        )
        _, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("batch processing failed" in (r.error_message or "") for r in results)
        )

    async def test_result_count_mismatch_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e3"}]
        consumer = _consumer_with_events(
            events,
            process_fn=_make_process_fn(results=[ProcessingResult(status="SUCCESS")]),
        )
        _, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("count mismatch" in (r.error_message or "") for r in results)
        )

    async def test_passes_filter_and_sort_to_reader(self):
        events = [{"event_id": "e1"}]
        consumer = _consumer_with_events(events)
        await consumer.handle_events(
            "automation_engine",
            "events",
            where="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )
        consumer._reader.fetch_records.assert_called_once_with(
            "automation_engine",
            "events",
            where="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )

    async def test_accepts_bound_method_as_process_fn(self):
        """Class-style users can still pass instance.method as process_fn."""

        class Worker:
            def __init__(self):
                self.received = []

            async def process_batch(self, events):
                self.received.append(events)
                return [ProcessingResult(status="SUCCESS") for _ in events]

        w = Worker()
        events = [{"event_id": "e1"}]
        consumer = _consumer_with_events(events, process_fn=w.process_batch)
        _, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual([r.status for r in results], ["SUCCESS"])
        self.assertEqual(w.received, [events])


class TestEventsConsumerSelfConstruction(unittest.IsolatedAsyncioTestCase):
    async def test_reader_is_built_from_env_on_first_call(self):
        """Reader is built lazily so callers can construct EventsConsumer
        before env is configured."""
        with patch(
            "application_sdk.lakehouse.events_consumer.LakehouseReader.from_env"
        ) as from_env:
            fake_reader = MagicMock()
            fake_reader.fetch_records = MagicMock(return_value=[])
            from_env.return_value = fake_reader

            consumer = EventsConsumer(_make_process_fn())
            self.assertIsNone(consumer._reader)
            from_env.assert_not_called()

            await consumer.handle_events("automation_engine", "events")
            from_env.assert_called_once()

            # Second call reuses the reader.
            await consumer.handle_events("automation_engine", "events")
            from_env.assert_called_once()


if __name__ == "__main__":
    unittest.main()

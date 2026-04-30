import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.events_consumer import EventsConsumer
from application_sdk.lakehouse.models import ProcessingResult


class FakeProcessor:
    def __init__(self, results=None, raise_in_process=False):
        self._results = results
        self._raise = raise_in_process
        self.setup_called = False
        self.teardown_called = False
        self.received: list[list[dict]] = []

    async def setup(self):
        self.setup_called = True

    async def teardown(self):
        self.teardown_called = True

    async def process_batch(self, events):
        self.received.append(events)
        if self._raise:
            raise RuntimeError("boom")
        if self._results is not None:
            return self._results
        return [ProcessingResult(status="SUCCESS") for _ in events]


def _consumer_with_events(events):
    """Build an EventsConsumer whose internal reader returns ``events``."""
    consumer = EventsConsumer(FakeProcessor())
    fake_reader = MagicMock()
    fake_reader.fetch_records = MagicMock(return_value=events)
    consumer._reader = fake_reader
    return consumer


class TestEventsConsumer(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_pair(self):
        consumer = _consumer_with_events([])
        events, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        self.assertFalse(consumer._processor.setup_called)
        self.assertFalse(consumer._processor.teardown_called)

    async def test_dispatches_events_to_processor(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        consumer = _consumer_with_events(events)
        out_events, results = await consumer.handle_events(
            "automation_engine", "events"
        )
        self.assertEqual(out_events, events)
        self.assertEqual([r.status for r in results], ["SUCCESS", "SUCCESS"])
        self.assertTrue(consumer._processor.setup_called)
        self.assertTrue(consumer._processor.teardown_called)

    async def test_processor_exception_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        consumer = EventsConsumer(FakeProcessor(raise_in_process=True))
        fake_reader = MagicMock()
        fake_reader.fetch_records = MagicMock(return_value=events)
        consumer._reader = fake_reader

        _, results = await consumer.handle_events("automation_engine", "events")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("batch processing failed" in (r.error_message or "") for r in results)
        )
        self.assertTrue(consumer._processor.teardown_called)

    async def test_result_count_mismatch_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e3"}]
        consumer = EventsConsumer(
            FakeProcessor(results=[ProcessingResult(status="SUCCESS")])
        )
        fake_reader = MagicMock()
        fake_reader.fetch_records = MagicMock(return_value=events)
        consumer._reader = fake_reader

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
            row_filter="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )
        consumer._reader.fetch_records.assert_called_once_with(
            "automation_engine",
            "events",
            row_filter="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )


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

            consumer = EventsConsumer(FakeProcessor())
            self.assertIsNone(consumer._reader)
            from_env.assert_not_called()

            await consumer.handle_events("automation_engine", "events")
            from_env.assert_called_once()

            # Second call reuses the reader.
            await consumer.handle_events("automation_engine", "events")
            from_env.assert_called_once()


if __name__ == "__main__":
    unittest.main()

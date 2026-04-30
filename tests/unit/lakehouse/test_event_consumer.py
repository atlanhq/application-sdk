import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse.event_consumer import EventTriggeredConsumer
from application_sdk.lakehouse.interface import LakehouseInterface
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


def _interface_with_events(events):
    interface = LakehouseInterface(MagicMock(), app_namespace="my_app")
    interface.reader.fetch_records = MagicMock(return_value=events)  # type: ignore[method-assign]
    return interface


class TestEventTriggeredConsumer(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_pair(self):
        interface = _interface_with_events([])
        processor = FakeProcessor()
        consumer = EventTriggeredConsumer(interface, processor)

        events, results = await consumer.handle_trigger("automation_engine", "events")
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        # setup/teardown not called when there's no work
        self.assertFalse(processor.setup_called)
        self.assertFalse(processor.teardown_called)

    async def test_dispatches_events_to_processor(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        interface = _interface_with_events(events)
        processor = FakeProcessor()
        consumer = EventTriggeredConsumer(interface, processor)

        out_events, results = await consumer.handle_trigger(
            "automation_engine", "events"
        )
        self.assertEqual(out_events, events)
        self.assertEqual([r.status for r in results], ["SUCCESS", "SUCCESS"])
        self.assertTrue(processor.setup_called)
        self.assertTrue(processor.teardown_called)

    async def test_processor_exception_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        interface = _interface_with_events(events)
        processor = FakeProcessor(raise_in_process=True)
        consumer = EventTriggeredConsumer(interface, processor)

        out_events, results = await consumer.handle_trigger(
            "automation_engine", "events"
        )
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("batch processing failed" in (r.error_message or "") for r in results)
        )
        # teardown still called
        self.assertTrue(processor.teardown_called)

    async def test_result_count_mismatch_marks_all_retry(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e3"}]
        interface = _interface_with_events(events)
        processor = FakeProcessor(results=[ProcessingResult(status="SUCCESS")])
        consumer = EventTriggeredConsumer(interface, processor)

        _, results = await consumer.handle_trigger("automation_engine", "events")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "RETRY" for r in results))
        self.assertTrue(
            all("count mismatch" in (r.error_message or "") for r in results)
        )

    async def test_passes_filter_and_sort_to_reader(self):
        events = [{"event_id": "e1"}]
        interface = _interface_with_events(events)
        processor = FakeProcessor()
        consumer = EventTriggeredConsumer(interface, processor)

        await consumer.handle_trigger(
            "automation_engine",
            "events",
            row_filter="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )
        interface.reader.fetch_records.assert_called_once_with(
            "automation_engine",
            "events",
            row_filter="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )

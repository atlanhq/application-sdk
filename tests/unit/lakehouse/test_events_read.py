import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.events_read import events_read
from application_sdk.lakehouse.models import EventResult


def _make_handler(results=None, raise_in_handler=False):
    """Build an async handler for tests, recording invocations."""
    state = {"calls": [], "raise": raise_in_handler}

    async def fn(events):
        state["calls"].append(events)
        if state["raise"]:
            raise RuntimeError("boom")
        if results is not None:
            return results
        return [EventResult(status="SUCCESS") for _ in events]

    fn.calls = state["calls"]  # type: ignore[attr-defined]
    return fn


def _patched_reader(events):
    """Patch LakehouseReader.from_env to return a reader yielding ``events``."""
    fake_reader = MagicMock()
    fake_reader.fetch_records = MagicMock(return_value=events)
    return patch(
        "application_sdk.lakehouse.events_read.LakehouseReader.from_env",
        return_value=fake_reader,
    ), fake_reader


class TestEventsRead(unittest.IsolatedAsyncioTestCase):
    async def test_no_events_returns_empty_pair_and_skips_handler(self):
        handler = _make_handler()
        ctx, _ = _patched_reader([])
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
        ctx, _ = _patched_reader(events)
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
        ctx, _ = _patched_reader(events)
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
        ctx, _ = _patched_reader(events)
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
        ctx, fake_reader = _patched_reader(events)
        with ctx:
            await events_read(
                namespace="automation_engine",
                table="events",
                handler=_make_handler(),
                where="status = 'pending'",
                limit=10,
                sort_by="ingested_at",
            )
        fake_reader.fetch_records.assert_called_once_with(
            "automation_engine",
            "events",
            where="status = 'pending'",
            limit=10,
            sort_by="ingested_at",
        )

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
        ctx, _ = _patched_reader(events)
        with ctx:
            _, results = await events_read(
                namespace="automation_engine",
                table="events",
                handler=w.process_batch,
            )
        self.assertEqual([r.status for r in results], ["SUCCESS"])
        self.assertEqual(w.received, [events])

    async def test_reader_built_from_env_each_call(self):
        """Reader is rebuilt each call — no cross-call shared state."""
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


if __name__ == "__main__":
    unittest.main()

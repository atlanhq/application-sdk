import unittest
from datetime import datetime, timezone

import pyarrow as pa

from application_sdk.lakehouse.work_query import WorkQueryRunner


def _make_arrow_table(rows: list[dict]) -> pa.Table:
    if not rows:
        schema = pa.schema(
            [
                ("event_id", pa.string()),
                ("asset_id", pa.string()),
                ("ingested_at", pa.timestamp("us", tz="UTC")),
                ("payload", pa.string()),
                ("status", pa.string()),
                ("kind", pa.string()),
                ("retry_count", pa.int64()),
                ("kafka_partition", pa.int64()),
                ("kafka_offset", pa.int64()),
                ("error_message", pa.string()),
            ]
        )
        return pa.table({name: [] for name in schema.names}, schema=schema)
    return pa.Table.from_pylist(rows)


class TestWorkQueryRunner(unittest.TestCase):
    def setUp(self):
        self.runner = WorkQueryRunner()

    def test_no_events_returns_empty(self):
        arrow = _make_arrow_table([])
        self.assertEqual(self.runner.run(arrow, limit=10), [])

    def test_single_pending_event_returned(self):
        arrow = _make_arrow_table(
            [
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": datetime.now(timezone.utc),
                    "payload": '{"desc": "hello"}',
                    "status": "PENDING",
                    "kind": "EVENT",
                    "retry_count": 0,
                    "kafka_partition": 0,
                    "kafka_offset": 1,
                    "error_message": None,
                }
            ]
        )
        result = self.runner.run(arrow, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_id"], "e1")
        self.assertEqual(result[0]["current_retry_count"], 0)

    def test_event_with_success_outcome_skipped(self):
        now = datetime.now(timezone.utc)
        arrow = _make_arrow_table(
            [
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": '{"x": 1}',
                    "status": "PENDING",
                    "kind": "EVENT",
                    "retry_count": 0,
                    "kafka_partition": 0,
                    "kafka_offset": 1,
                    "error_message": None,
                },
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": None,
                    "status": "SUCCESS",
                    "kind": "OUTCOME",
                    "retry_count": 0,
                    "kafka_partition": None,
                    "kafka_offset": None,
                    "error_message": None,
                },
            ]
        )
        self.assertEqual(len(self.runner.run(arrow, limit=10)), 0)

    def test_event_with_retry_outcome_returned(self):
        now = datetime.now(timezone.utc)
        arrow = _make_arrow_table(
            [
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": '{"x": 1}',
                    "status": "PENDING",
                    "kind": "EVENT",
                    "retry_count": 0,
                    "kafka_partition": 0,
                    "kafka_offset": 1,
                    "error_message": None,
                },
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": None,
                    "status": "RETRY",
                    "kind": "OUTCOME",
                    "retry_count": 2,
                    "kafka_partition": None,
                    "kafka_offset": None,
                    "error_message": "timeout",
                },
            ]
        )
        result = self.runner.run(arrow, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["current_retry_count"], 2)

    def test_max_retries_exceeded_skipped(self):
        now = datetime.now(timezone.utc)
        arrow = _make_arrow_table(
            [
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": '{"x": 1}',
                    "status": "PENDING",
                    "kind": "EVENT",
                    "retry_count": 0,
                    "kafka_partition": 0,
                    "kafka_offset": 1,
                    "error_message": None,
                },
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": None,
                    "status": "RETRY",
                    "kind": "OUTCOME",
                    "retry_count": 5,
                    "kafka_partition": None,
                    "kafka_offset": None,
                    "error_message": "timeout",
                },
            ]
        )
        self.assertEqual(len(self.runner.run(arrow, limit=10)), 0)

    def test_newer_event_supersedes_old_success(self):
        now = datetime.now(timezone.utc)
        arrow = _make_arrow_table(
            [
                {
                    "event_id": "e2",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": '{"new": true}',
                    "status": "PENDING",
                    "kind": "EVENT",
                    "retry_count": 0,
                    "kafka_partition": 0,
                    "kafka_offset": 2,
                    "error_message": None,
                },
                {
                    "event_id": "e1",
                    "asset_id": "a1",
                    "ingested_at": now,
                    "payload": None,
                    "status": "SUCCESS",
                    "kind": "OUTCOME",
                    "retry_count": 0,
                    "kafka_partition": None,
                    "kafka_offset": None,
                    "error_message": None,
                },
            ]
        )
        result = self.runner.run(arrow, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["event_id"], "e2")

    def test_limit_respected(self):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "event_id": f"e{i}",
                "asset_id": f"a{i}",
                "ingested_at": now,
                "payload": "{}",
                "status": "PENDING",
                "kind": "EVENT",
                "retry_count": 0,
                "kafka_partition": 0,
                "kafka_offset": i,
                "error_message": None,
            }
            for i in range(5)
        ]
        arrow = _make_arrow_table(rows)
        self.assertEqual(len(self.runner.run(arrow, limit=3)), 3)

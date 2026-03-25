import unittest


class TestSourceTagInfo(unittest.TestCase):
    def test_create_source_tag_info(self):
        from application_sdk.common.reverse_sync.models import SourceTagInfo

        tag = SourceTagInfo(
            source_tag_name="MY_TAG",
            source_tag_qualified_name="default/snowflake/123/DB/SCHEMA/MY_TAG",
            source_tag_guid="guid-1",
            source_tag_connector_name="snowflake",
            tag_value="val1",
        )
        self.assertEqual(tag.source_tag_name, "MY_TAG")
        self.assertEqual(tag.tag_value, "val1")

    def test_write_back_sql(self):
        from application_sdk.common.reverse_sync.models import WriteBackSQL

        sql = WriteBackSQL(
            statement='ALTER Table "DB"."S"."T" SET TAG "DB"."S"."TAG" = \'v\'',
            operation="SET",
        )
        self.assertIn("SET TAG", sql.statement)

    def test_reverse_sync_result(self):
        from application_sdk.common.reverse_sync.models import ReverseSyncResult

        result = ReverseSyncResult(total=50, passed=45, synced=40, failed=2, skipped=3)
        self.assertTrue(result.has_failures)
        self.assertEqual(result.success_rate, 40 / 50)

    def test_reverse_sync_result_no_failures(self):
        from application_sdk.common.reverse_sync.models import ReverseSyncResult

        result = ReverseSyncResult(total=10, passed=10, synced=10, failed=0, skipped=0)
        self.assertFalse(result.has_failures)
        self.assertEqual(result.success_rate, 1.0)

    def test_reverse_sync_result_empty(self):
        from application_sdk.common.reverse_sync.models import ReverseSyncResult

        result = ReverseSyncResult()
        self.assertFalse(result.has_failures)
        self.assertEqual(result.success_rate, 0.0)

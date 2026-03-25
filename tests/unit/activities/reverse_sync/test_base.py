import json
import unittest


class TestBaseReverseSyncProcessRow(unittest.TestCase):
    def test_parse_row_extracts_tags(self):
        from application_sdk.activities.reverse_sync.base import (
            BaseReverseSyncActivities,
        )

        row = {
            "operation_type": "CLASSIFICATION_ADD",
            "mutated_details_json": json.dumps(
                [
                    {
                        "typeName": "t1",
                        "attributes": {
                            "k1": [
                                {
                                    "sourceTagName": "TAG",
                                    "sourceTagQualifiedName": "default/snowflake/123/DB/S/TAG",
                                    "sourceTagGuid": "g1",
                                    "sourceTagConnectorName": "snowflake",
                                    "sourceTagValue": [{"tagAttachmentValue": "v1"}],
                                }
                            ]
                        },
                    }
                ]
            ),
        }
        tags = BaseReverseSyncActivities._parse_source_tags(
            row, connector_filter="snowflake"
        )
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].source_tag_name, "TAG")
        self.assertEqual(tags[0].tag_value, "v1")

    def test_parse_row_no_tags_returns_empty(self):
        from application_sdk.activities.reverse_sync.base import (
            BaseReverseSyncActivities,
        )

        row = {
            "operation_type": "CLASSIFICATION_ADD",
            "mutated_details_json": json.dumps([]),
        }
        tags = BaseReverseSyncActivities._parse_source_tags(
            row, connector_filter="snowflake"
        )
        self.assertEqual(len(tags), 0)

    def test_parse_row_filters_by_connector(self):
        from application_sdk.activities.reverse_sync.base import (
            BaseReverseSyncActivities,
        )

        row = {
            "operation_type": "CLASSIFICATION_ADD",
            "mutated_details_json": json.dumps(
                [
                    {
                        "typeName": "t1",
                        "attributes": {
                            "k1": [
                                {
                                    "sourceTagName": "TAG",
                                    "sourceTagQualifiedName": "qn",
                                    "sourceTagGuid": "g",
                                    "sourceTagConnectorName": "databricks",
                                    "sourceTagValue": [{"tagAttachmentValue": "v1"}],
                                }
                            ]
                        },
                    }
                ]
            ),
        }
        tags = BaseReverseSyncActivities._parse_source_tags(
            row, connector_filter="snowflake"
        )
        self.assertEqual(len(tags), 0)

    def test_parse_row_missing_json_returns_empty(self):
        from application_sdk.activities.reverse_sync.base import (
            BaseReverseSyncActivities,
        )

        row = {"operation_type": "CLASSIFICATION_ADD"}
        tags = BaseReverseSyncActivities._parse_source_tags(row)
        self.assertEqual(len(tags), 0)

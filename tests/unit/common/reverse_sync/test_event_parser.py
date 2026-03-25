import unittest


class TestExtractSourceTags(unittest.TestCase):
    def test_nested_format_with_attributes_wrapper(self):
        """Format: {typeName: "SourceTagAttachment", attributes: {sourceTagName: ...}}"""
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        mutated_details = [
            {
                "typeName": "rpcUfAZkuqFWfZcrhtucoi",
                "attributes": {
                    "brEoXNmBNClm1L6wfPrQex": [
                        {
                            "typeName": "SourceTagAttachment",
                            "attributes": {
                                "sourceTagName": "SF_TAG",
                                "sourceTagQualifiedName": "default/snowflake/123/DB/SCHEMA/SF_TAG",
                                "sourceTagGuid": "guid-1",
                                "sourceTagConnectorName": "snowflake",
                                "sourceTagValue": [
                                    {
                                        "typeName": "SourceTagAttachmentValue",
                                        "attributes": {"tagAttachmentValue": "b-tag"},
                                    }
                                ],
                            },
                        }
                    ]
                },
            }
        ]
        tags = extract_source_tags(mutated_details)
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].source_tag_name, "SF_TAG")
        self.assertEqual(tags[0].tag_value, "b-tag")
        self.assertEqual(tags[0].source_tag_connector_name, "snowflake")

    def test_flat_format_without_wrapper(self):
        """Format: {sourceTagName: ..., sourceTagValue: [{tagAttachmentValue: ...}]}"""
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        mutated_details = [
            {
                "typeName": "b1y5RZzm7ZsoBpKk9J8qBQ",
                "attributes": {
                    "O3TJ2MN3HmkrqFe2gzcDt5": [
                        {
                            "sourceTagGuid": "753ebd50",
                            "sourceTagName": "SF_TABLE_TAG",
                            "sourceTagQualifiedName": "default/snowflake/123/DB/SCHEMA/SF_TABLE_TAG",
                            "sourceTagConnectorName": "snowflake",
                            "sourceTagValue": [{"tagAttachmentValue": "tagv12"}],
                        }
                    ]
                },
            }
        ]
        tags = extract_source_tags(mutated_details)
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].source_tag_name, "SF_TABLE_TAG")
        self.assertEqual(tags[0].tag_value, "tagv12")

    def test_multiple_mutated_details(self):
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        mutated_details = [
            {
                "typeName": "t1",
                "attributes": {
                    "k1": [
                        {
                            "sourceTagName": "TAG_A",
                            "sourceTagQualifiedName": "qn/A",
                            "sourceTagGuid": "g1",
                            "sourceTagConnectorName": "snowflake",
                            "sourceTagValue": [{"tagAttachmentValue": "v1"}],
                        }
                    ]
                },
            },
            {
                "typeName": "t2",
                "attributes": {
                    "k2": [
                        {
                            "sourceTagName": "TAG_B",
                            "sourceTagQualifiedName": "qn/B",
                            "sourceTagGuid": "g2",
                            "sourceTagConnectorName": "snowflake",
                            "sourceTagValue": [{"tagAttachmentValue": "v2"}],
                        }
                    ]
                },
            },
        ]
        tags = extract_source_tags(mutated_details)
        self.assertEqual(len(tags), 2)

    def test_filter_by_connector(self):
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        mutated_details = [
            {
                "typeName": "t1",
                "attributes": {
                    "k1": [
                        {
                            "sourceTagName": "TAG",
                            "sourceTagQualifiedName": "qn",
                            "sourceTagGuid": "g",
                            "sourceTagConnectorName": "databricks",
                            "sourceTagValue": [],
                        }
                    ]
                },
            },
        ]
        tags = extract_source_tags(mutated_details, connector_filter="snowflake")
        self.assertEqual(len(tags), 0)

    def test_empty_mutated_details(self):
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        self.assertEqual(extract_source_tags([]), [])

    def test_missing_source_tag_value(self):
        from application_sdk.common.reverse_sync.event_parser import extract_source_tags

        mutated_details = [
            {
                "typeName": "t1",
                "attributes": {
                    "k1": [
                        {
                            "sourceTagName": "TAG",
                            "sourceTagQualifiedName": "qn",
                            "sourceTagGuid": "g",
                            "sourceTagConnectorName": "snowflake",
                            "sourceTagValue": [],
                        }
                    ]
                },
            }
        ]
        tags = extract_source_tags(mutated_details)
        self.assertEqual(tags[0].tag_value, "")

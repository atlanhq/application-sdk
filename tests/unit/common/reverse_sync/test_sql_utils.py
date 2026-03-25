import unittest


class TestObjectNameFromQN(unittest.TestCase):
    def test_snowflake_table(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn(
            "default/snowflake/123/DB/SCHEMA/TABLE", quote_char='"'
        )
        self.assertEqual(result, '"DB"."SCHEMA"."TABLE"')

    def test_snowflake_tag(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn(
            "default/snowflake/123/DB/SCHEMA/MY_TAG", quote_char='"'
        )
        self.assertEqual(result, '"DB"."SCHEMA"."MY_TAG"')

    def test_databricks_table(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn(
            "default/databricks/456/catalog/schema/table", quote_char="`"
        )
        self.assertEqual(result, "`catalog`.`schema`.`table`")

    def test_schema_two_parts(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn("default/snowflake/123/DB/SCHEMA", quote_char='"')
        self.assertEqual(result, '"DB"."SCHEMA"')

    def test_database_one_part(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn("default/snowflake/123/DB", quote_char='"')
        self.assertEqual(result, '"DB"')

    def test_too_short_returns_empty(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        self.assertEqual(object_name_from_qn("default/snowflake", quote_char='"'), "")

    def test_custom_skip_parts(self):
        from application_sdk.common.reverse_sync.sql_utils import object_name_from_qn

        result = object_name_from_qn(
            "a/b/c/d/DB/SCHEMA/TABLE", quote_char='"', skip_parts=4
        )
        self.assertEqual(result, '"DB"."SCHEMA"."TABLE"')


class TestEscapeSqlValue(unittest.TestCase):
    def test_escape_single_quote(self):
        from application_sdk.common.reverse_sync.sql_utils import escape_sql_value

        self.assertEqual(escape_sql_value("it's a tag"), "it''s a tag")

    def test_no_escape_needed(self):
        from application_sdk.common.reverse_sync.sql_utils import escape_sql_value

        self.assertEqual(escape_sql_value("normal_value"), "normal_value")


class TestConvertEntityType(unittest.TestCase):
    def test_snowflake_stream_strips_prefix(self):
        from application_sdk.common.reverse_sync.sql_utils import (
            convert_entity_type_to_sql,
        )

        self.assertEqual(convert_entity_type_to_sql("SnowflakeStream"), "Stream")

    def test_view_stays_view(self):
        from application_sdk.common.reverse_sync.sql_utils import (
            convert_entity_type_to_sql,
        )

        self.assertEqual(convert_entity_type_to_sql("View"), "View")

    def test_materialised_view_becomes_view(self):
        from application_sdk.common.reverse_sync.sql_utils import (
            convert_entity_type_to_sql,
        )

        self.assertEqual(convert_entity_type_to_sql("MaterialisedView"), "View")

    def test_table_stays_table(self):
        from application_sdk.common.reverse_sync.sql_utils import (
            convert_entity_type_to_sql,
        )

        self.assertEqual(convert_entity_type_to_sql("Table"), "Table")

    def test_unknown_type_passes_through(self):
        from application_sdk.common.reverse_sync.sql_utils import (
            convert_entity_type_to_sql,
        )

        self.assertEqual(convert_entity_type_to_sql("UnknownType"), "UnknownType")

import unittest
from codecs import ignore_errors

import daft
from application_sdk.activities.atlan.diff import DiffAtlanActivities
from hashlib import md5
import psutil


class DiffActivitiesTest(unittest.TestCase):

    def test_create_current_state_hash(self):
        diff = DiffAtlanActivities()

        df = daft.from_pylist(
            [
                {
                    "typeName": "Table",
                    "attributes": {
                        "qualifiedName": "default.table1",
                        "name": "table1",
                        "owner": "Alice",
                        "integers": 1,
                        "structs": {"a": 1},
                        "maps": {"a": 1},
                    },
                    "customAttributes": {
                        "description": "table1 description",
                    },
                    "classifications": {
                        "classification": "PII",
                    },
                    "terms": {
                        "term": "term1",
                    },
                }
            ]
        )
        hash_df = diff.create_current_state_hash(df)
        results = hash_df.to_pylist()

        self.assertEqual(len(results), 1)
        from xxhash import xxh3_64

        result = results[0]
        expected_result = {
            "typeName": "Table",
            "qualifiedName": "default.table1",
            "attributes_hash": xxh3_64("1table1Alice").intdigest(),
            "custom_attributes_hash": xxh3_64("table1 description").intdigest(),
            "classifications_hash": xxh3_64("PII").intdigest(),
            "terms_hash": xxh3_64("term1").intdigest(),
        }
        self.assertEqual(result, expected_result)

    def test_compute_string_hash_without_ignore_cols(self):
        df = daft.from_pylist(
            [
                {"id": 1, "name": "Alice", "age": 23},
                {"id": 2, "name": "Bob", "age": 25},
            ]
        )

        diff = DiffAtlanActivities()
        result_df = diff._compute_row_hash(df)
        results = result_df.to_pylist()

        self.assertEqual(results[0]["row_hash"], md5(b"231Alice").hexdigest())
        self.assertEqual(results[1]["row_hash"], md5(b"252Bob").hexdigest())

    def test_compute_string_hash_with_ignore_cols(self):
        df = daft.from_pylist(
            [
                {"id": 1, "name": "Alice", "age": 23},
                {"id": 2, "name": "Bob", "age": 25},
            ]
        )

        diff = DiffAtlanActivities()
        result_df = diff._compute_row_hash(df, ignore_columns=["age"])
        results = result_df.to_pylist()

        self.assertEqual(results[0]["row_hash"], md5(b"1Alice").hexdigest())
        self.assertEqual(results[1]["row_hash"], md5(b"2Bob").hexdigest())

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
        result = results[0]
        expected_result = {
            "typeName": "Table",
            "qualifiedName": "default.table1",
            "attributes_hash": md5(b"table1Alice").hexdigest(),
            "custom_attributes_hash": md5(b"table1 description").hexdigest(),
            "classifications_hash": md5(b"PII").hexdigest(),
            "terms_hash": md5(b"term1").hexdigest(),
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

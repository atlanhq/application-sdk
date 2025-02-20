import unittest
import daft
from application_sdk.activities.atlan.diff import DiffActivities
from hashlib import md5
import psutil


class DiffActivitiesTest(unittest.TestCase):
    def test_compute_string_hash_without_ignore_cols(self):
        df = daft.from_pylist(
            [
                {"id": 1, "name": "Alice", "age": 23},
                {"id": 2, "name": "Bob", "age": 25},
            ]
        )

        diff = DiffActivities({})
        result_df = diff._compute_row_hash(df, [col.name() for col in df.columns])
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

        diff = DiffActivities({})
        result_df = diff._compute_row_hash(df, [col.name() for col in df.columns], ["age"])
        results = result_df.to_pylist()

        self.assertEqual(results[0]["row_hash"], md5(b"1Alice").hexdigest())
        self.assertEqual(results[1]["row_hash"], md5(b"2Bob").hexdigest())

    def test_calculate_diff_basic(self):
        df1 = daft.from_pylist(
            [
                {"id": 1, "name": "Alice", "age": 23},
                {"id": 2, "name": "Bob", "age": 25},
            ]
        )

        df2 = daft.from_pylist(
            [
                {"id": 1, "name": "Alice2", "age": 23},
                {"id": 3, "name": "Charlie", "age": 30},
            ]
        )

        diff = DiffActivities({})
        result = diff.calculate_diff(df1, df2, ["id"])
        results = {key: result[key].to_pylist() for key in result}
        self.assertEqual(results["added"], [{"id": 3, "name": "Charlie", "age": 30}])
        self.assertEqual(results["removed"], [{"id": 2, "name": "Bob", "age": 25}])
        self.assertEqual(results["modified"], [{"id": 1, "name": "Alice2", "age": 23}])


    def test_compute_string_hash_100k(self):
        df = daft.from_pylist(
            [
                {"id": i, "name": f"Alice{i}", "age": 23 + i} for i in range(1000000)
            ]
        )

        # Check memory usage
        diff = DiffActivities({})

        start_memory = psutil.virtual_memory()[3]/1000000000
        result_df = diff._compute_row_hash(df, [col.name() for col in df.columns])
        end_memory = psutil.virtual_memory()[3]/1000000000
        print(f"Memory used: {end_memory - start_memory}")

        self.assertEqual(len(result_df.to_pylist()), 100000)

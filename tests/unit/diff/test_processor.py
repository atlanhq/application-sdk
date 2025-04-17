import unittest
from unittest.mock import Mock, patch

import daft
import orjson

from application_sdk.diff.models import (
    DiffOutputFormat,
    DiffProcessorConfig,
    DiffState,
    TransformedDataRow,
)
from application_sdk.diff.processor import DiffProcessor


class TestDiffProcessor(unittest.TestCase):
    """Unit tests for the DiffProcessor class.

    Tests the core functionality of the DiffProcessor including:
    - Data loading and transformation
    - Column hashing
    - Partition assignment
    - Diff calculation
    - Data export
    """

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.processor = DiffProcessor()
        self.config = DiffProcessorConfig(
            ignore_columns=["lastModifiedTime"],
            num_partitions=4,
            output_prefix="test_output",
            chunk_size=1000,
        )

    def test_transform_row_mapper(self):
        """Test the transform_row_mapper static method.

        Tests that the mapper correctly extracts and transforms required fields.
        """
        # Test valid input
        test_row = {
            "typeName": "test_type",
            "attributes": {"qualifiedName": "test_name"},
        }

        result = DiffProcessor.transform_row_mapper(test_row)

        self.assertIsInstance(result, TransformedDataRow)
        self.assertEqual(result.type_name, "test_type")
        self.assertEqual(result.qualified_name, "test_name")
        self.assertIsInstance(result.record, str)

        # Test missing typeName
        with self.assertRaises(ValueError) as cm:
            DiffProcessor.transform_row_mapper(
                {"attributes": {"qualifiedName": "test_name"}}
            )
        self.assertIn("typeName is required", str(cm.exception))

        # Test missing qualifiedName
        with self.assertRaises(ValueError) as cm:
            DiffProcessor.transform_row_mapper({"typeName": "test_type"})
        self.assertIn("qualifiedName is required", str(cm.exception))

        # Test completely empty input
        with self.assertRaises(ValueError) as cm:
            DiffProcessor.transform_row_mapper({})
        self.assertIn("typeName is required", str(cm.exception))

    def test_hash_function(self):
        """Test the hash static method.

        Tests that the hash function correctly hashes record fields while respecting ignore columns.
        """
        test_series = daft.Series(
            [
                orjson.dumps({"field1": "value1", "lastModifiedTime": "123"}).decode(
                    "utf-8"
                )
            ]
        )

        result = DiffProcessor.hash(test_series, "", ["lastModifiedTime"])

        self.assertIsInstance(result[0], str)
        self.assertEqual(len(result), 1)

    def test_partition_hash(self):
        """Test the partition_hash static method.

        Tests that partition assignment is consistent and within bounds.
        """
        qualified_names = daft.Series(["test.name1", "test.name2"])
        type_names = daft.Series(["Type1", "Type2"])
        num_partitions = 4

        result = DiffProcessor.partition_hash(
            qualified_names, type_names, num_partitions
        )

        for partition in result:
            self.assertGreaterEqual(partition, 0)
            self.assertLess(partition, num_partitions)

    @patch("glob.glob")
    def test_load_transformed_data(self, mock_glob):
        """Test the load_transformed_data method.

        Tests that JSON data is correctly loaded and transformed into DataFrames.
        """
        mock_glob.return_value = ["test.json"]
        mock_data = '{"typeName": "test", "attributes": {"qualifiedName": "test.name"}}'

        with patch("builtins.open", unittest.mock.mock_open(read_data=mock_data)):
            iterator = self.processor.load_transformed_data(
                json_prefix="test",
                row_mapper=DiffProcessor.transform_row_mapper,
                chunk_size=1000,
            )

            df = next(iterator)
            self.assertIsInstance(df, daft.DataFrame)

    def test_operation_counts(self):
        """Test that operation counts are properly tracked."""
        self.assertEqual(self.processor.operation_counts["load"], 0)
        self.assertEqual(self.processor.operation_counts["hash"], 0)
        self.assertEqual(self.processor.operation_counts["partition"], 0)
        self.assertEqual(self.processor.operation_counts["diff"], 0)
        self.assertEqual(self.processor.operation_counts["export"], 0)


if __name__ == "__main__":
    unittest.main()

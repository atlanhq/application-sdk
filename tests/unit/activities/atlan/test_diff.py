from unittest.mock import patch

import daft
import pytest

from application_sdk.activities.atlan.diff import DiffCalculator


class TestDiffCalculator:
    @pytest.fixture
    def sample_df(self) -> daft.DataFrame:
        # Create a simple DataFrame with test data
        df = daft.from_pydict({"col2": ["a"], "col3": [True], "col1": [1]})  # type: ignore
        return df

    def test_compute_row_hash_basic(self, sample_df: daft.DataFrame) -> None:
        # Mock the SQL execution
        with patch("daft.sql") as mock_sql:
            # Setup the mock to return the input DataFrame
            mock_sql.return_value = sample_df

            # Call the function
            DiffCalculator.compute_row_hash(sample_df)

            # Verify SQL was called with correct parameters
            mock_sql.assert_called_once()
            sql_query = mock_sql.call_args[0][0]
            assert "HASH(CONCAT" in sql_query
            assert "CAST(col1 AS STRING)" in sql_query
            assert "CAST(col2 AS STRING)" in sql_query
            assert "CAST(col3 AS STRING)" in sql_query
            assert "AS row_hash" in sql_query

    def test_compute_row_hash_custom_column_name(
        self, sample_df: daft.DataFrame
    ) -> None:
        with patch("daft.sql") as mock_sql:
            mock_sql.return_value = sample_df

            DiffCalculator.compute_row_hash(
                sample_df, row_hash_column_name="custom_hash"
            )

            mock_sql.assert_called_once()
            sql_query = mock_sql.call_args[0][0]
            assert "AS custom_hash" in sql_query

    async def test_compute_row_hash_sorting(self, sample_df: daft.DataFrame) -> None:
        hashed_df = DiffCalculator.compute_row_hash(
            sample_df,
        ).collect()

        sorted_value = daft.sql(
            "SELECT HASH(CONCAT(col1, col2, col3)) AS row_hash from hashed_df"
        ).to_pydict()["row_hash"][0]
        print(sorted_value)
        assert sorted_value == hashed_df.select("row_hash").to_pydict()["row_hash"][0]

        hashed_df = DiffCalculator.compute_row_hash(sample_df, sort_columns=False)

        unsorted_value = daft.sql(
            "SELECT HASH(CONCAT(col2, col3, col1)) AS row_hash from hashed_df"
        ).to_pydict()["row_hash"][0]
        assert unsorted_value == hashed_df.select("row_hash").to_pydict()["row_hash"][0]

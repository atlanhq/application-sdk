import daft
import duckdb
from application_sdk.activities import ActivitiesInterface


class DiffActivities(ActivitiesInterface):

    @staticmethod
    def _compute_row_hash(df: daft.DataFrame, columns_order: list[str], ignore_columns: list[str] = None) -> daft.DataFrame:
        """
        Create a hashed column for the dataframe
        Args:
            df: DataFrame to create hashed column
            columns_order: Order of columns to be used for hashing
            ignore_columns: Columns to ignore while creating the hash
        Returns:
            DataFrame with hashed column
        """
        if ignore_columns is None:
            ignore_columns = []

        df_cols = [col.name() for col in df.columns]
        cols_to_hash = [col for col in columns_order if col not in ignore_columns or col not in df_cols]

        arrow_df = df.to_arrow()
        # Create a concatenated string of values for hashing
        concat_expr = ",".join(cols_to_hash)

        # Query in duckdb
        results = duckdb.sql(f"SELECT *, md5(concat({concat_expr})) as row_hash FROM arrow_df").arrow()
        return daft.from_arrow(results)


    def calculate_diff(self,
                       old_df: daft.DataFrame,
                       new_df: daft.DataFrame,
                       key_columns: list[str],
                       ignore_columns: list[str] = None,
                       ) -> dict[str, daft.DataFrame]:

        column_order = [col.name() for col in old_df.columns]
        df1_with_hash = self._compute_row_hash(old_df, column_order, ignore_columns).to_arrow()
        df2_with_hash = self._compute_row_hash(new_df, column_order, ignore_columns).to_arrow()

        # Perform outer join on key columns
        new_entries_df = duckdb.sql(f"""
            SELECT df2.* EXCLUDE(row_hash) FROM df2_with_hash as df2
            LEFT JOIN df1_with_hash
            ON {f" AND ".join([f"df1_with_hash.{col} = df2.{col}" for col in key_columns])}
            WHERE df1_with_hash.row_hash IS NULL
        """).arrow()

        removed_entries_df = duckdb.sql(f"""
            SELECT df1.* EXCLUDE(row_hash) FROM df1_with_hash as df1
            LEFT JOIN df2_with_hash
            ON {f" AND ".join([f"df1.{col} = df2_with_hash.{col}" for col in key_columns])}
            WHERE df2_with_hash.row_hash IS NULL
        """).arrow()

        modified_entries_df = duckdb.sql(f"""
            SELECT df2.* EXCLUDE(row_hash) FROM df1_with_hash as df1
            JOIN df2_with_hash df2
            ON {f" AND ".join([f"df1.{col} = df2.{col}" for col in key_columns])}
            WHERE df1.row_hash != df2.row_hash
        """).arrow()

        changes = {
            "added": daft.from_arrow(new_entries_df),
            "removed": daft.from_arrow(removed_entries_df),
            "modified": daft.from_arrow(modified_entries_df),
        }
        return changes

from typing import Dict, Any

import daft
import duckdb
from application_sdk.activities import ActivitiesInterface, ActivitiesState, get_workflow_id
from temporalio import activity


class DiffActivitiesState(ActivitiesState):
    circuit_breaker_check: bool = True


class DiffActivities(ActivitiesInterface):

    def __init__(self, workflow_args: dict):
        super().__init__()
        self.workflow_args = workflow_args

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            circuit_breaker_check = workflow_args.get("circuit_breaker_check", True)
            self._state[workflow_id] = DiffActivitiesState(workflow_args=workflow_args,
                                                           circuit_breaker_check=circuit_breaker_check)
        return


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
            "original": old_df,
        }
        return changes

    def delete_circuit_breaker_check(self, changes: Dict[str, daft.DataFrame],
                                     threshold: float=80.0) -> bool:

        check = self._get_state(self.workflow_args).circut_breaker_check
        if not check:
            return False

        delete_changes = changes["removed"]
        original_count = changes["original"].count_rows()
        delete_count = delete_changes.count_rows()
        if original_count == 0:
            return False

        delete_percentage = (delete_count / original_count) * 100
        if delete_percentage > threshold:
            return True
        return False


    @activity.defn
    def calculate_atlas_diff(self, old_df: daft.DataFrame, new_df: daft.DataFrame) -> dict:
        attribute_changes = self.calculate_diff(
            old_df.select("typeName", "attributes.*"),
            new_df.select("typeName", "attributes.*"),
            ["typeName", "qualifiedName"],
            ["lastSyncWorkflowName", "lastSyncRunAt", "lastSyncRun"]
        )

        custom_attribute_changes = self.calculate_diff(
            old_df.select("typeName", "customAttributes.*"),
            new_df.select("typeName", "customAttributes.*"),
            ["typeName", "qualifiedName"],
            ["lastWorkflowName"]
        )

        classification_changes = self.calculate_diff(
            old_df.select("typeName", "classifications.*"),
            new_df.select("typeName", "classifications.*"),
            ["typeName", "qualifiedName"],
            ["lastWorkflowName"]
        )

        return {
            "attributes": {
                "added": attribute_changes["added"].count_rows(),
                "removed": attribute_changes["removed"].count_rows(),
                "modified": attribute_changes["modified"].count_rows(),
            },
            "customAttributes": {
                "added": custom_attribute_changes["added"].count_rows(),
                "removed": custom_attribute_changes["removed"].count_rows(),
                "modified": custom_attribute_changes["modified"].count_rows(),
            },
            "classifications": {
                "added": classification_changes["added"].count_rows(),
                "removed": classification_changes["removed"].count_rows(),
                "modified": classification_changes["modified"].count_rows(),
            }
        }


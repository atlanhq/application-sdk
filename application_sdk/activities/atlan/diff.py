import daft
import duckdb
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


class DiffAtlanActivities:
    IGNORE_COLUMNS = ["lastSyncWorkflowName", "lastSyncRunAt", "lastSyncRun", "typeName", "qualifiedName"]

    @classmethod
    def create_current_state_hash(cls, df: daft.DataFrame) -> daft.DataFrame:
        """
        Create a hash of the current state of the metadata
        Args:
            df: DataFrame to create hash
        Returns:
            DataFrame with hashed columns for attributes, customAttributes, terms and classifications
            Columns - typeName, qualifiedName, attributes_hash, custom_attributes_hash, classifications_hash, terms_hash
        """
        logger.debug(f"Creating current state hash")

        # Create hashes for attributes, customAttributes, terms and classifications
        attributes_df = df.select("typeName", "attributes.*")
        attributes_hash_df = cls._compute_row_hash(
            attributes_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        attributes_hash_df = attributes_hash_df.with_column_renamed("row_hash", "attributes_hash")

        custom_attributes_df = df.select("typeName", "attributes.qualifiedName", "customAttributes.*")
        custom_attributes_hash_df = cls._compute_row_hash(
            custom_attributes_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        custom_attributes_hash_df = custom_attributes_hash_df.with_column_renamed("row_hash", "custom_attributes_hash")

        classifications_df = df.select("typeName", "attributes.qualifiedName", "classifications.*")
        classifications_hash_df = cls._compute_row_hash(
            classifications_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        classifications_hash_df = classifications_hash_df.with_column_renamed("row_hash", "classifications_hash")

        terms_df = df.select("typeName", "attributes.qualifiedName", "terms.*")
        terms_hash_df = cls._compute_row_hash(
            terms_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        terms_hash_df = terms_hash_df.with_column_renamed("row_hash", "terms_hash")

        hash_df = attributes_hash_df.join(custom_attributes_hash_df, on=["typeName", "qualifiedName"]) \
            .join(classifications_hash_df, on=["typeName", "qualifiedName"]) \
            .join(terms_hash_df, on=["typeName", "qualifiedName"])

        return hash_df

    @staticmethod
    def _compute_row_hash(df: daft.DataFrame, columns_order: list[str] = None, ignore_columns: list[str] = None) -> daft.DataFrame:
        """
        Create a hashed column for the dataframe
        Args:
            df: DataFrame to create hashed column
            columns_order: Order of columns to be used for hashing
            ignore_columns: Columns to ignore while creating the hash
        Returns:
            DataFrame with hashed column
        """
        if columns_order is None:
            columns_order = [col.name() for col in df.columns]
            columns_order.sort()

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

    @activity.defn
    def calculate_diff_test(self):
        logger.info("Calculating diff")



import daft
import duckdb
from daft import col
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
        attributes_df = df.select("typeName", col("attributes").struct.get("*"))
        attributes_hash_df = cls._compute_row_hash(
            attributes_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        attributes_hash_df = attributes_hash_df.with_column_renamed("row_hash", "attributes_hash")

        custom_attributes_df = df.select("typeName", col("attributes").struct.get("qualifiedName"),
                                         col("customAttributes").struct.get("*"))
        custom_attributes_hash_df = cls._compute_row_hash(
            custom_attributes_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        custom_attributes_hash_df = custom_attributes_hash_df.with_column_renamed("row_hash", "custom_attributes_hash")

        classifications_df = df.select("typeName", col("attributes").struct.get("qualifiedName"),
                                       col("classifications").struct.get("*"))
        classifications_hash_df = cls._compute_row_hash(
            classifications_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        classifications_hash_df = classifications_hash_df.with_column_renamed("row_hash", "classifications_hash")

        terms_df = df.select("typeName", col("attributes").struct.get("qualifiedName"), col("terms").struct.get("*"))
        terms_hash_df = cls._compute_row_hash(
            terms_df, ignore_columns=cls.IGNORE_COLUMNS).select("typeName", "qualifiedName", "row_hash")
        terms_hash_df = terms_hash_df.with_column_renamed("row_hash", "terms_hash")

        hash_df = attributes_hash_df.join(custom_attributes_hash_df, on=["typeName", "qualifiedName"]) \
            .join(classifications_hash_df, on=["typeName", "qualifiedName"]) \
            .join(terms_hash_df, on=["typeName", "qualifiedName"])

        return hash_df

    @staticmethod
    def _compute_row_hash(df: daft.DataFrame,
                          columns_order: list[str] = None,
                          ignore_columns: list[str] = None) -> daft.DataFrame:
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

        if columns_order is None:
            columns_order = [column.name() for column in df.columns]
            columns_order.sort()

        cols_to_hash = [column for column in columns_order if column not in ignore_columns]
        concat_expr = ",".join([f"""CAST({column} AS STRING)""" for column in cols_to_hash])

        # Add a new column with the hash value
        df = daft.sql(f"""
            SELECT *, HASH(CONCAT({concat_expr})) AS row_hash FROM df
        """)
        return df

    @activity.defn
    def calculate_diff_test(self):
        logger.info("Calculating diff")



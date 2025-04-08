import enum
import glob
import os
from collections import defaultdict
from typing import List, Optional

import daft
from daft import col
from temporalio import activity

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.json import JsonInput
from application_sdk.outputs.json import JsonOutput

logger = get_logger(__name__)


class DiffState(enum.Enum):
    """
    Enum to represent the state of an entity in the diff.
    """

    NEW = "NEW"
    NO_DIFF = "NO_DIFF"
    UPDATED = "UPDATED"
    DELETED = "DELETED"


class DiffCalculator:
    def __init__(
        self,
        current_state: Optional[daft.DataFrame],
        transformed_data: defaultdict[str, List[daft.DataFrame]],
        ignore_attributes: List[str] = [],
        circuit_breaker_percentage: float = 100,
        use_append_pattern: bool = False,
        output_prefix: str = "diff",
        enable_audit_logging: bool = False,
    ):
        logger.info("Initializing DiffCalculator")
        self.current_state = current_state
        self.transformed_data = transformed_data
        self.use_append_pattern = use_append_pattern
        self.ignore_attributes = ignore_attributes
        self.circuit_breaker_percentage = circuit_breaker_percentage
        self.output_prefix = output_prefix
        self.enable_audit_logging = enable_audit_logging
        logger.info(
            f"DiffCalculator initialized with ignore_attributes={ignore_attributes}, circuit_breaker_percentage={circuit_breaker_percentage}"
        )

    async def calculate(self):
        logger.info("Starting diff calculation")
        transformed_data_with_hash = await self.add_hash_columns(
            transformed_data=self.transformed_data["/column"][0],
            columns_to_ignore=self.ignore_attributes,
        )
        logger.info("Hash columns added to transformed data")

        output_handler = JsonOutput(  # type: ignore
            output_suffix="",
            output_path="/Users/junaid/atlan/debug/postgres/mosaic/diff",
        )
        logger.info(f"Writing transformed data to {output_handler}")
        output_handler.write_daft_dataframe(dataframe=transformed_data_with_hash)  # type: ignore
        logger.info("Transformed data written successfully")

        if not self.current_state:
            logger.info("No current state provided. Marking all entities as new.")

        if self.use_append_pattern:
            logger.info("Using append pattern")
            pass

    async def calculate_diff(
        self,
        transformed_data_with_hashes: daft.DataFrame,
        current_state: daft.DataFrame,
    ) -> daft.DataFrame:
        """
        Calculate the diff between transformed data and current state.

        This function compares two DataFrames to identify changes between them. It performs a full outer
        join on typeName and qualifiedName columns and determines the diff state for each entity.

        Args:
            transformed_data_with_hashes (daft.DataFrame): DataFrame containing the new/transformed data
                with hash columns already computed.
            current_state (daft.DataFrame): DataFrame containing the current state data with hash
                columns already computed.

        Returns:
            daft.DataFrame: A DataFrame containing diff results with columns:
                - typeName: The type of the entity
                - qualifiedName: Unique identifier for the entity
                - diffHash: Hash value representing the entity state
                - diffState: One of NEW, DELETED, UPDATED, or NO_DIFF indicating the change status
        """
        logger.info("Calculating diff between transformed data and current state")

        result = daft.sql(f"""
            SELECT
                t.typeName,
                t.qualifiedName,
                t.diffHash,
                CASE
                    WHEN c.diffHash IS NULL THEN '{DiffState.NEW.value}'
                    WHEN t.diffHash IS NULL THEN '{DiffState.DELETED.value}'
                    WHEN t.diffHash = c.diffHash THEN '{DiffState.NO_DIFF.value}'
                    ELSE '{DiffState.UPDATED.value}'
                END AS diffState
            FROM transformed_data_with_hashes t
            FULL OUTER JOIN current_state c
                ON t.typeName = c.typeName
                AND t.qualifiedName = c.qualifiedName
        """)
        logger.info("Diff calculation completed")
        return result

    async def generate_current_state(
        self, transformed_data: daft.DataFrame
    ) -> daft.DataFrame:
        logger.info("Generating current state")
        transformed_data = transformed_data.select(
            col("attributes").struct.get("qualifiedName"), col("*")
        )
        logger.info("Selected required columns for current state")

        hash_df = await self.add_hash_columns(
            transformed_data=transformed_data, columns_to_ignore=self.ignore_attributes
        )
        logger.info("Added hash columns to current state")

        result = transformed_data.join(hash_df, on=["typeName", "qualifiedName"])
        logger.info("Current state generation completed")
        return result

    async def add_hash_columns(
        self, transformed_data: daft.DataFrame, columns_to_ignore: List[str]
    ) -> daft.DataFrame:
        """Adds hash columns to the input DataFrame by computing hashes of different attribute groups.

        This function takes a DataFrame with nested attributes and computes hash values for different
        groups of attributes (attributes, customAttributes, classifications, terms) to enable diffing.

        Input DataFrame:
        +-----------+------------------+-------------------+------------------+--------------+
        | typeName  | attributes       | customAttributes  | classifications  | terms        |
        +-----------+------------------+-------------------+------------------+--------------+
        | Table     | {col1: val1,     | {custom1: cval1}  | {class1: true}   | {term1: 1}   |
        |           |  col2: val2}     |                   |                  |              |
        +-----------+------------------+-------------------+------------------+--------------+

        After hashing each group:
        +-----------+---------------+----------------+----------------------+---------------------+------------------+
        | typeName  | qualifiedName | attributesHash | customAttributesHash | classificationsHash | termsHash        |
        +-----------+---------------+----------------+----------------------+---------------------+------------------+
        | Table     | db.table1     | hash1...       | hash2...             | hash3...            | hash4...         |
        +-----------+---------------+----------------+----------------------+---------------------+------------------+

        Final output with diffHash:

        +-----------+---------------+----------------+----------------------+---------------------+------------------+-------------+
        | typeName  | qualifiedName | attributesHash | customAttributesHash | classificationsHash | termsHash        | diffHash    |
        +-----------+---------------+----------------+----------------------+---------------------+------------------+-------------+
        | Table     | db.table1     | hash1...       | hash2...             | hash3...            | hash4...         | hash5...    |
        +-----------+---------------+----------------+----------------------+---------------------+------------------+-------------+

        Args:
            transformed_data: Input DataFrame containing nested attribute columns
            columns_to_ignore: List of column names to exclude from hash computation

        Returns:
            DataFrame with added hash columns for each attribute group and a final diffHash
        """
        logger.info("Adding hash columns to DataFrame")
        logger.info(f"{transformed_data}")

        keys_to_hash = ["attributes", "customAttributes", "classifications", "terms"]
        hash_dataframes: List[daft.DataFrame] = []

        for key in keys_to_hash:
            logger.info(f"Processing hash for {key}")
            if key not in transformed_data.column_names:
                continue

            if key == "attributes":
                key_df = transformed_data.select(
                    col("typeName"),
                    col("attributes").struct.get("*"),
                )
            else:
                key_df = transformed_data.select(
                    col("typeName"),
                    col("attributes").struct.get("qualifiedName"),
                    col(key).struct.get("*"),
                )

            key_hash_df = self.compute_row_hash(
                key_df,
                columns_to_ignore=columns_to_ignore,
                row_hash_column_name=f"{key}Hash",
            )
            hash_dataframes.append(key_hash_df)
            logger.info(f"Hash computation completed for {key}")

        result = hash_dataframes[0]
        for hash_df in hash_dataframes[1:]:
            result = result.join(hash_df, on=["typeName", "qualifiedName"])
        logger.info("Joined all hash DataFrames")

        # Combine typeName, qualifiedName and all hash columns to compute diffHash
        diff_hash_columns: List[str] = []
        diff_hash_columns.append("CAST(typeName AS STRING)")
        diff_hash_columns.append("CAST(qualifiedName AS STRING)")

        for key in keys_to_hash:
            if key in transformed_data.column_names:
                diff_hash_columns.append(f"CAST({key}Hash AS STRING)")

        result = daft.sql(f"""
            SELECT *, HASH(CONCAT({",".join(diff_hash_columns)})) AS diffHash FROM result
        """)
        logger.info("Hash columns added successfully")
        return result

    @staticmethod
    def compute_row_hash(
        df: daft.DataFrame,
        columns_to_ignore: List[str] = [],
        sort_columns: bool = True,
        row_hash_column_name: str = "row_hash",
    ) -> daft.DataFrame:
        """
        Create a hashed column for the dataframe
        Args:
            df: DataFrame to create hashed column
            columns_order: Order of columns to be used for hashing
            ignore_columns: Columns to ignore while creating the hash
        Returns:
            DataFrame with hashed column
        """
        logger.info(f"Computing row hash with {len(columns_to_ignore)} ignored columns")

        columns = [column.name() for column in df.columns]
        columns = list(filter(lambda c: c not in columns_to_ignore, columns))

        if sort_columns:
            columns.sort()
            logger.info("Columns sorted for consistent hashing")

        concat_expr = ",".join([f"""CAST({column} AS STRING)""" for column in columns])

        df = daft.sql(f"""
            SELECT *, HASH(CONCAT({concat_expr})) AS {row_hash_column_name} FROM df
        """)
        logger.info(f"Row hash computed and stored in column {row_hash_column_name}")
        return df


class DiffAtlanActivities(ActivitiesInterface):
    IGNORE_COLUMNS = [
        "lastSyncWorkflowName",
        "lastSyncRunAt",
        "lastSyncRun",
        "typeName",
        "qualifiedName",
    ]

    @activity.defn
    async def calculate_diff(self):
        logger.info("Starting diff calculation activity")
        current_run_path = "/Users/junaid/atlan/debug/postgres/mosaic/atlan-postgres-1688410509-cron-1743963660/atlan-postgres-1688410509-cron-1743963660/7f4a49cb-dcab-474b-b39c-1c842761c400"
        logger.info(f"Using current run path: {current_run_path}")

        transformed_data: defaultdict[str, List[daft.DataFrame]] = defaultdict(list)

        file_pattern = f"{current_run_path}/transformed/**/*.json"
        logger.info(f"Searching for files matching pattern: {file_pattern}")

        for file in glob.glob(file_pattern, recursive=True):
            logger.info(f"Processing file: {file}")
            prefix = os.path.dirname(file).split("/transformed")[-1]
            transformed_data[prefix].append(
                await JsonInput(  # type: ignore
                    path=os.path.dirname(file), file_names=[os.path.basename(file)]
                ).get_daft_dataframe()
            )
            logger.info(f"Added DataFrame for prefix: {prefix}")

        logger.info(
            f"Found {sum(len(dfs) for dfs in transformed_data.values())} files across {len(transformed_data)} prefixes"
        )

        diff_calculator = DiffCalculator(
            current_state=None,
            transformed_data=transformed_data,
            ignore_attributes=self.IGNORE_COLUMNS,
        )
        result = await diff_calculator.calculate()
        logger.info("Diff calculation activity completed")
        return result

import glob
import os
import random
from typing import Any, Callable, Dict, Iterator, List

import daft
import orjson
import pydantic
import xxhash

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import get_value_for_attr
from application_sdk.diff.models import (
    DiffOutputFormat,
    DiffProcessorConfig,
    DiffState,
    TransformedDataRow,
)


class DiffProcessor:
    def __init__(self):
        self.logger = get_logger(__name__)
        self.operation_counts = {
            "load": 0,
            "hash": 0,
            "partition": 0,
            "diff": 0,
            "export": 0,
        }

    def process(self, transformed_data_prefix: str, config: DiffProcessorConfig):
        transformed_data_iter = self.load_transformed_data(
            json_prefix=transformed_data_prefix,
            row_mapper=self.transform_row_mapper,
            chunk_size=config.chunk_size,
        )

        hashed_data_iter = self.hash_columns(
            df_iterator=transformed_data_iter,
            hash_udf=self.hash,
            ignore_columns=config.ignore_columns,
        )

        partitioned_data_iter = self.assign_partitions(
            df_iterator=hashed_data_iter,
            num_partitions=config.num_partitions,
            partiton_hash_udf=self.partition_hash,
        )

        diff_data_iter = self.calculate_diff(
            df_iterator=partitioned_data_iter,
        )

        os.makedirs(config.output_prefix, exist_ok=True)
        self.export_data(
            df_iterator=diff_data_iter,
            format=DiffOutputFormat.PARQUET,
            output_prefix=config.output_prefix,
        )

    @staticmethod
    def transform_row_mapper(row: Dict[str, Any]) -> TransformedDataRow:
        type_name = get_value_for_attr(row, "typeName")
        if not type_name:
            raise ValueError("typeName is required")

        qualified_name = get_value_for_attr(row, "attributes/qualifiedName")
        if not qualified_name:
            raise ValueError("qualifiedName is required")

        return TransformedDataRow(
            type_name=type_name,  # type: ignore
            qualified_name=qualified_name,  # type: ignore
            record=orjson.dumps(row, option=orjson.OPT_SORT_KEYS).decode("utf-8"),
        )

    def load_transformed_data(
        self,
        json_prefix: str,
        row_mapper: Callable[[Dict[str, Any]], pydantic.BaseModel],
        chunk_size: int = 1000,
    ) -> Iterator[daft.DataFrame]:
        """
        Load data from a prefix of JSON files into an iterator of daft dataframes.

        Args:
            json_prefix (str): The prefix path for JSON files.
            file_loader (Callable): A function to load files, injected for testing.

        Returns:
            Iterator[daft.DataFrame]: An iterator of daft dataframes.
        """
        json_files = glob.glob(f"{json_prefix}/**/*.json", recursive=True)

        for json_file in json_files:
            try:
                with open(json_file, "r") as f:
                    records = []
                    for line in f:
                        if len(records) >= chunk_size:
                            df = daft.from_pylist(records)
                            yield df
                            records = []

                        record = orjson.loads(line)
                        records.append(row_mapper(record).model_dump())

                    # Handle any remaining records
                    if records:
                        df = daft.from_pylist(records)
                        yield df

            except Exception as e:
                self.logger.error(f"Error processing file {json_file}: {str(e)}")
                continue

    @staticmethod
    @daft.udf(return_dtype=daft.DataType.string())
    def hash(
        col: daft.Series, sub_key_to_hash: str, ignore_keys: List[str]
    ) -> List[str]:
        """
        Default hash function using xxh3_128 and orjson.

        Args:
            col (daft.Series): Column to hash.

        Returns:
            List[str]: List of hashed values.
        """
        hashed_values = []
        for row in col:
            row_dict = orjson.loads(row)
            row_dict = get_value_for_attr(row_dict, sub_key_to_hash)

            if not row_dict:
                hashed_values.append(None)
                continue

            if ignore_keys:
                for key in ignore_keys:
                    row_dict.pop(key, None)

            hashed_values.append(
                xxhash.xxh3_128(
                    orjson.dumps(row_dict, option=orjson.OPT_SORT_KEYS)
                ).hexdigest()
            )

        return hashed_values

    @staticmethod
    @daft.udf(return_dtype=daft.DataType.int64())
    def partition_hash(
        qualified_names: daft.Series, type_names: daft.Series, num_partitions: int = 4
    ) -> List[int]:
        """
        Generate partition assignments using consistent hashing based on qualified name and type.

        Ensures records with the same qualified name + type combination are assigned to the same
        partition for parallel processing.

        Args:
            qualified_name (daft.Series): Series containing qualified names
            type_name (daft.Series): Series containing type names
            num_partitions (int, optional): Number of partitions. Defaults to 4.

        Returns:
            List[int]: List of partition numbers (0 to num_partitions-1) for each record

        Example:
            >>> partition_hash(["db.table1", "db.table2"], ["Table", "Table"], 4)
            [2, 1]  # Records consistently hashed to partitions 2 and 1
        """
        partition_assignments = []

        for qualified_name, type_name in zip(qualified_names, type_names):
            # Combine qualified name and type
            key = f"{type_name}/{qualified_name}"

            # Generate hash and map to partition
            hash_val = xxhash.xxh3_128(key.encode()).intdigest()
            partition = abs(hash_val) % num_partitions
            partition_assignments.append(partition)

        return partition_assignments

    def hash_columns(
        self,
        df_iterator: Iterator[daft.DataFrame],
        hash_udf: Callable,
        ignore_columns: List[str],
    ) -> Iterator[daft.DataFrame]:
        """
        Apply hashing to specific columns in the dataframe.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to process.
            hash_function (Callable): Hash function to use, injected for testing.

        Returns:
            Iterator[daft.DataFrame]: An iterator of daft dataframes with hashed columns.
        """
        self.logger.info("Hashing columns")
        self.operation_counts["hash"] += 1

        for df in df_iterator:
            # Apply the hash function to the specified columns
            df = df.with_columns(
                {
                    "attributes_hash": hash_udf(
                        df["record"], "attributes", ignore_columns
                    ),
                    "custom_attributes_hash": hash_udf(
                        df["record"], "customAttributes", ignore_columns
                    ),
                    "business_attributes_hash": hash_udf(
                        df["record"], "businessAttributes", ignore_columns
                    ),
                    "relationship_attributes_hash": hash_udf(
                        df["record"], "relationshipAttributes", ignore_columns
                    ),
                    "classifications_hash": hash_udf(
                        df["record"], "classifications", ignore_columns
                    ),
                    "terms_hash": hash_udf(df["record"], "terms", ignore_columns),
                    "full_hash": hash_udf(df["record"], "", ignore_columns),
                }
            )
            yield df

    def assign_partitions(
        self,
        df_iterator: Iterator[daft.DataFrame],
        num_partitions: int,
        partiton_hash_udf: Callable,
    ) -> Iterator[daft.DataFrame]:
        """
        Partition data based on consistent hashing.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to partition.
            num_partitions (int): Number of partitions.
            partition_function (Callable): Partition function to use, injected for testing.

        Returns:
            Iterator[daft.DataFrame]: An iterator of partitioned daft dataframes.
        """
        self.logger.info("Assigning partitions")
        self.operation_counts["partition"] += 1

        for df in df_iterator:
            df = df.with_columns(
                {
                    "partition": partiton_hash_udf(
                        df["qualified_name"], df["type_name"], num_partitions
                    ),
                }
            )

            yield df

    # Diff Calculation

    def calculate_diff(
        self,
        df_iterator: Iterator[daft.DataFrame],
    ) -> Iterator[daft.DataFrame]:
        """
        Calculate differences between records.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to process.

        Returns:
            Iterator[daft.DataFrame]: An iterator of daft dataframes with diff states.
        """
        # TODO: Implement diff calculation
        self.logger.info("Calculating diff")
        self.operation_counts["diff"] += 1

        for df in df_iterator:
            df = df.with_columns(
                {
                    "diff_status": daft.lit(
                        random.choice([state.value for state in DiffState])
                    ),
                    "diff_changes": daft.lit("TO BE IMPLEMENTED"),
                }
            )
            yield df

    # Output

    def export_data(
        self,
        df_iterator: Iterator[daft.DataFrame],
        format: DiffOutputFormat,
        output_prefix: str,
    ) -> None:
        """
        Export data in specified format.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to export.
            format_key (str): Format key, either `json` or `parquet`.
            output_prefix (str): Output path prefix.
            export_function (Callable): Export function to use, injected for testing.

        Returns:
            None
        """
        """
        Export data in specified format with Hive partitioning.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to export
            format (DiffOutputFormat): Output format (JSON or PARQUET)
            output_prefix (str): Base output path prefix

        Example transformation:
        Input DataFrame:
        +----------+-------------+--------+-------------+
        | typeName | diffStatus | record | partition   |
        +----------+-------------+--------+-------------+
        | Person   | NEW        | {...}  | 2          |
        | Item     | DIFF       | {...}  | 1          |
        +----------+-------------+--------+-------------+

        Output directory structure:
        output_prefix/
        ├── typeName=Person/
        │   └── partition=2/
        │       └── data.json/parquet
        └── typeName=Item/
            └── partition=1/
                └── data.json/parquet
        """
        self.logger.info(f"Exporting data in {format.value} format")
        self.operation_counts["export"] += 1

        for df in df_iterator:
            if format == DiffOutputFormat.PARQUET:
                df.write_parquet(
                    output_prefix,
                    partition_cols=["diff_status", "type_name"],
                    write_mode="append",
                )
            else:
                raise ValueError(f"Unsupported output format: {format}")

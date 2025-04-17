import glob
import os
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import daft
import orjson
import pydantic

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import get_value_for_attr, hash
from application_sdk.diff.models import (
    DiffOutputFormat,
    DiffProcessorConfig,
    DiffState,
    PublishStateRow,
    TransformedDataRow,
)

HashColumnToKeyMap = {
    "attributes_hash": "attributes",
    "custom_attributes_hash": "customAttributes",
    "business_attributes_hash": "businessAttributes",
    "relationship_attributes_hash": "relationshipAttributes",
    "classifications_hash": "classifications",
    "terms_hash": "terms",
}


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

    def process(
        self,
        transformed_data_prefix: str,
        publish_state_prefix: str,
        config: DiffProcessorConfig,
    ):
        publish_state_df = self.load_publish_state(
            publish_state_prefix=publish_state_prefix,
        )

        publish_state_df = self.add_hash_columns(
            df=publish_state_df,
            ignore_columns=config.ignore_columns,
        )

        publish_state_df = self.assign_partitions(
            df=publish_state_df,
            num_partitions=config.num_partitions,
        )

        self.export_data(
            df_iterator=iter([publish_state_df]),
            format=DiffOutputFormat.PARQUET,
            output_prefix="/Users/junaid/atlan/debug/postgres/mosaic/publish-state",
            partition_cols=["type_name"],
        )

        transformed_data_iter = self.load_transformed_data(
            json_prefix=transformed_data_prefix,
            transform_fn=self.transform_row_mapper,
            chunk_size=config.chunk_size,
        )

        diff_data_iter = self.diff_pipeline(
            df_iterator=transformed_data_iter,
            publish_state_df=publish_state_df,
            config=config,
        )

        os.makedirs(config.output_prefix, exist_ok=True)
        self.export_data(
            df_iterator=diff_data_iter,
            format=DiffOutputFormat.PARQUET,
            output_prefix=config.output_prefix,
            partition_cols=["diff_status", "type_name"],
        )

    def diff_pipeline(
        self,
        df_iterator: Iterator[daft.DataFrame],
        publish_state_df: daft.DataFrame,
        config: DiffProcessorConfig,
    ) -> Iterator[daft.DataFrame]:
        for df in df_iterator:
            df = self.add_hash_columns(
                df=df,
                ignore_columns=config.ignore_columns,
            )

            df = self.assign_partitions(
                df=df,
                num_partitions=config.num_partitions,
            )

            df = self.calculate_diff(
                df=df,
                publish_state_df=publish_state_df,
            )

            yield df

    @staticmethod
    def sanitize_entity(
        entity: Dict[str, Any], ignore_attributes_keys: List[str]
    ) -> Dict[str, Any]:
        if "attributes" not in entity:
            return entity

        for key in ignore_attributes_keys:
            if key in entity["attributes"]:
                entity["attributes"].pop(key, None)

        return entity

    @staticmethod
    def transform_row_mapper(row: Dict[str, Any]) -> TransformedDataRow:
        type_name = get_value_for_attr(row, "typeName")
        if not type_name:
            raise ValueError("typeName is required")

        qualified_name = get_value_for_attr(row, "attributes/qualifiedName")
        if not qualified_name:
            raise ValueError("qualifiedName is required")

        return TransformedDataRow(
            type_name=type_name,
            qualified_name=qualified_name,
            entity=orjson.dumps(row).decode("utf-8"),
        )

    @staticmethod
    @daft.udf(
        return_dtype=daft.DataType.struct(
            fields={k: daft.DataType.string() for k in HashColumnToKeyMap.keys()}
        )
    )
    def granular_hash_udf(
        entities: daft.Series, ignore_attributes_keys: List[str]
    ) -> List[Dict[str, Optional[str]]]:
        """
        Daft UDF to hash a series of dictionaries using xxhash.

        Args:
            col (daft.Series): Column to hash.
            ignore_keys (List[str]): The keys to ignore.

        Returns:
            List[str]: List of hashed values.
        """
        result: List[Dict[str, Optional[str]]] = []

        for entity in entities:
            entity_hash: Dict[str, Optional[str]] = {
                k: None for k in HashColumnToKeyMap.keys()
            }

            entity_dict = orjson.loads(entity)
            if not entity_dict:
                continue

            entity_dict = DiffProcessor.sanitize_entity(
                entity_dict, ignore_attributes_keys
            )

            for hash_column, key in HashColumnToKeyMap.items():
                value = get_value_for_attr(entity_dict, key)

                if not value:
                    continue

                entity_hash[hash_column] = hash(value).hexdigest()

            result.append(entity_hash)

        return result

    @staticmethod
    @daft.udf(return_dtype=daft.DataType.string())
    def full_hash_udf(
        entities: daft.Series,
        ignore_attributes_keys: List[str],
    ) -> List[str]:
        result = []

        for entity in entities:
            entity_dict = orjson.loads(entity)
            entity_dict = DiffProcessor.sanitize_entity(
                entity_dict, ignore_attributes_keys
            )

            result.append(hash(entity_dict).hexdigest())

        return result

    @staticmethod
    @daft.udf(return_dtype=daft.DataType.int64())
    def assign_partitions_udf(
        qualified_names: daft.Series,
        type_names: daft.Series,
        num_partitions: int = 4,
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

            partition = abs(hash(key).intdigest()) % num_partitions
            partition_assignments.append(partition)

        return partition_assignments

    def load_publish_state(self, publish_state_prefix: str) -> daft.DataFrame:
        """
        Load publish state from a prefix of JSON files into a daft dataframe.

        Args:
            publish_state_prefix (str): The prefix path for publish state files.

        Returns:
            daft.DataFrame: A daft dataframe.
        """
        json_files = glob.glob(f"{publish_state_prefix}/**/*.json", recursive=True)

        records = []

        for json_file in json_files:
            with open(json_file, "r") as f:
                for line in f:
                    entity = orjson.loads(line)

                    record = PublishStateRow(
                        type_name=entity["typeName"],
                        qualified_name=entity["attributes"]["qualifiedName"],
                        entity=orjson.dumps(entity).decode("utf-8"),
                        publish_status="SUCCESS",
                        app_error_code=None,
                        metastore_error_code=None,
                        runbook_url=None,
                    )

                    records.append(record.model_dump())

        return daft.from_pylist(records)

    def load_transformed_data(
        self,
        json_prefix: str,
        transform_fn: Callable[[Dict[str, Any]], pydantic.BaseModel],
        chunk_size: int,
    ) -> Iterator[daft.DataFrame]:
        """
        Load data from a prefix of JSON files into an iterator of daft dataframes.

        Args:
            json_prefix (str): The prefix path for JSON files.
            transform_fn (Callable): A function to transform the data.
            chunk_size (int, optional): The number of records to load into a dataframe.

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
                        records.append(transform_fn(record).model_dump())

                    # Handle any remaining records
                    if records:
                        df = daft.from_pylist(records)
                        yield df

            except Exception as e:
                self.logger.error(f"Error processing file {json_file}: {str(e)}")
                continue

    def add_hash_columns(
        self,
        df: daft.DataFrame,
        ignore_columns: List[str],
    ) -> daft.DataFrame:
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

        df = df.with_columns(
            {
                "granular_hash": self.granular_hash_udf(df["entity"], ignore_columns),
                "full_hash": self.full_hash_udf(df["entity"], ignore_columns),
            }
        )

        return df

    def assign_partitions(
        self,
        df: daft.DataFrame,
        num_partitions: int,
    ) -> daft.DataFrame:
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

        df = df.with_columns(
            {
                "partition": self.assign_partitions_udf(
                    df["qualified_name"], df["type_name"], num_partitions
                ),
            }
        )

        return df

    @staticmethod
    def diff_attributes(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"attributes": "changed"}

    @staticmethod
    def diff_custom_attributes(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"custom_attributes": "changed"}

    @staticmethod
    def diff_business_attributes(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"business_attributes": "changed"}

    @staticmethod
    def diff_relationship_attributes(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"relationship_attributes": "changed"}

    @staticmethod
    def diff_classifications(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"classifications": "changed"}

    @staticmethod
    def diff_terms(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"terms": "changed"}

    @staticmethod
    def diff(
        old_entity: Dict[str, Any], new_entity: Dict[str, Any], key: str
    ) -> Dict[str, Any]:
        if key == "attributes":
            return DiffProcessor.diff_attributes(old_entity, new_entity)
        elif key == "customAttributes":
            return DiffProcessor.diff_custom_attributes(old_entity, new_entity)
        elif key == "businessAttributes":
            return DiffProcessor.diff_business_attributes(old_entity, new_entity)
        elif key == "relationshipAttributes":
            return DiffProcessor.diff_relationship_attributes(old_entity, new_entity)
        elif key == "classifications":
            return DiffProcessor.diff_classifications(old_entity, new_entity)
        elif key == "terms":
            return DiffProcessor.diff_terms(old_entity, new_entity)

        raise ValueError(f"Unsupported key: {key}")

    @staticmethod
    @daft.udf(return_dtype=daft.DataType.string())
    def calculate_diff_udf(
        granular_hashes: daft.Series,
        old_granular_hashes: daft.Series,
        entities: daft.Series,
        old_entities: daft.Series,
    ) -> List[str]:
        """
        Calculate detailed diff between current and old records.

        Args:
            granular_hash (daft.Series): Hash of current record's attributes
            old_granular_hash (daft.Series): Hash of old record's attributes
            entity (daft.Series): Current record JSON string
            old_entity (daft.Series): Old record JSON string

        Returns:
            List[str]: List of diff records indicating which attributes changed

        Example:
            Input:
            granular_hash: {"attributes_hash": "abc", "custom_attributes_hash": "123"}
            old_granular_hash: {"attributes_hash": "xyz", "custom_attributes_hash": "123"}
            entity: {"attributes": {"name": "new"}, "customAttributes": {"owner": "team1"}}
            old_entity: {"attributes": {"name": "old"}, "customAttributes": {"owner": "team1"}}

            Output:
            "attributes: changed"
        """
        results = []

        for old_entity, new_entity, old_granular_hash, new_granular_hash in zip(
            old_entities, entities, old_granular_hashes, granular_hashes
        ):
            for key, entity_key in HashColumnToKeyMap.items():
                if old_granular_hash[key] != new_granular_hash[key]:
                    results.append(
                        orjson.dumps(
                            DiffProcessor.diff(
                                old_entity=old_entity,
                                new_entity=new_entity,
                                key=entity_key,
                            )
                        ).decode("utf-8")
                    )

        return results

    def calculate_diff(
        self,
        df: daft.DataFrame,
        publish_state_df: daft.DataFrame,
    ) -> daft.DataFrame:
        """
        Calculate differences between records.

        Args:
            df_iterator (Iterator[daft.DataFrame]): Iterator of dataframes to process.

        Returns:
            Iterator[daft.DataFrame]: An iterator of daft dataframes with diff states.
        """
        self.logger.info("Calculating diff")
        self.operation_counts["diff"] += 1

        df = daft.sql(f"""
            SELECT
                current.*,
                old.entity as old_entity,
                old.granular_hash as old_granular_hash,
                CASE
                    WHEN old.full_hash IS NULL THEN '{DiffState.NEW.value}'
                    WHEN current.full_hash IS NULL THEN '{DiffState.DELETE.value}'
                    WHEN current.full_hash = old.full_hash THEN '{DiffState.NO_DIFF.value}'
                    ELSE '{DiffState.DIFF.value}'
                END AS diff_status
            FROM df current
            LEFT JOIN publish_state_df old
                ON current.type_name = old.type_name
                AND current.qualified_name = old.qualified_name
        """)

        # df = df.with_columns(
        #     {
        #         "diff_record": DiffProcessor.calculate_diff_udf(
        #             df["granular_hash"],
        #             df["old_granular_hash"],
        #             df["entity"],
        #             df["old_entity"],
        #         ),
        #     }
        # )

        return df

    # Output
    def export_data(
        self,
        df_iterator: Iterator[daft.DataFrame],
        partition_cols: Sequence[str],
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
                    partition_cols=list(partition_cols),
                    write_mode="overwrite",  # TODO: Change to append
                )
            else:
                raise ValueError(f"Unsupported output format: {format}")

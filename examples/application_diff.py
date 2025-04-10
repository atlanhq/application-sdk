from application_sdk.diff.models import DiffProcessorConfig
from application_sdk.diff.processor import DiffProcessor


def main():
    """
    Example script demonstrating usage of DiffProcessor.

    Processes transformed data files to:
    1. Load and chunk data from JSON files
    2. Hash record attributes for comparison
    3. Partition data for parallel processing
    4. Calculate diffs between records
    5. Export results in Parquet format

    Example input JSON:
    {
        "typeName": "Table",
        "attributes": {
            "qualifiedName": "db.table1",
            "name": "table1",
            "description": "A table"
        },
        "customAttributes": {
            "owner": "team1"
        }
    }
    """
    # Configure the diff processor
    config = DiffProcessorConfig(
        ignore_columns=[],  # Ignore these fields when hashing
        num_partitions=4,  # Split data into 4 partitions
        output_prefix="/Users/junaid/atlan/debug/postgres/mosaic/diff",  # Write results here
        chunk_size=10,
    )

    # Initialize processor
    processor = DiffProcessor()

    # Process transformed data files
    processor.process(
        transformed_data_prefix="/Users/junaid/atlan/debug/postgres/mosaic/atlan-postgres-1688410509-cron-1742926860/atlan-postgres-1688410509-cron-1742926860/4ac936b2-bde1-4a7c-b726-799942f1d6ea/transformed",
        config=config,
    )


if __name__ == "__main__":
    main()

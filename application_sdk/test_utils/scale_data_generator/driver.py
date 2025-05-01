import argparse
import os
import shutil
import time
from dataclasses import dataclass

from loguru import logger

from application_sdk.test_utils.scale_data_generator.config_loader import (
    ConfigLoader,
    OutputFormat,
)
from application_sdk.test_utils.scale_data_generator.data_generator import DataGenerator


@dataclass
class DriverArgs:
    config_path: str
    output_format: str
    output_dir: str


def driver(args: DriverArgs):
    # cleanup output directory
    if os.path.exists(args.output_dir):
        shutil.rmtree(args.output_dir)

    try:
        start_time = time.time()

        # Initialize config loader and validate configuration
        config_loader = ConfigLoader(args.config_path)

        # Initialize data generator
        generator = DataGenerator(config_loader)

        # Generate data in specified format
        output_format = OutputFormat(args.output_format)
        generator.generate_data(output_format, args.output_dir)

        # Generate DuckDB databases with the data
        generator.generate_duckdb_tables(args.output_dir)

        end_time = time.time()

        # Calculate statistics
        hierarchy = config_loader.get_hierarchy()
        total_databases = hierarchy["records"]
        total_schemas = total_databases * next(
            child["records"]
            for child in hierarchy["children"]
            if child["name"] == "schemata"
        )
        schema_node = next(
            child for child in hierarchy["children"] if child["name"] == "schemata"
        )
        total_tables = total_schemas * sum(
            child["records"]
            for child in schema_node["children"]
            if child["name"] in ["tables", "views"]
        )
        total_columns = total_tables * next(
            child["records"]
            for child in schema_node["children"][0]["children"]
            if child["name"] == "columns"
        )

        logger.info(
            f"Data generation completed successfully:\n"
            f"- Format: {args.output_format}\n"
            f"- Output directory: {args.output_dir}\n"
            f"- Total databases: {total_databases}\n"
            f"- Total schemas: {total_schemas}\n"
            f"- Total tables/views: {total_tables}\n"
            f"- Total columns: {total_columns}\n"
            f"- Time taken: {end_time - start_time:.2f} seconds"
        )

    except Exception as e:
        logger.error(f"Error generating data: {e}")
        raise e


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate scaled test data based on configuration"
    )
    parser.add_argument(
        "--config-path",
        help="Path to the YAML configuration file",
        default="application_sdk/test_utils/scale_data_generator/examples/config.yaml",
    )
    parser.add_argument(
        "--output-format",
        choices=["csv", "json", "parquet"],
        help="Output format for generated data",
        default="json",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to save generated files",
        default="application_sdk/test_utils/scale_data_generator/output",
    )

    args = parser.parse_args()
    driver(DriverArgs(**vars(args)))

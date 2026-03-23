import argparse
import os
import shutil
import time
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.test_utils.scale_data_generator.config_loader import ConfigLoader
from application_sdk.test_utils.scale_data_generator.data_generator import DataGenerator

logger = get_logger(__name__)


@dataclass
class DriverArgs:
    config_path: str
    output_format: str
    output_dir: str


def driver(args: DriverArgs):
    # cleanup output directory
    if os.path.exists(args.output_dir):
        shutil.rmtree(args.output_dir)

    start_time = time.time()
    config_loader = ConfigLoader(args.config_path)
    generator = DataGenerator(config_loader)
    generator.generate_data(args.output_format, args.output_dir)
    generator.generate_duckdb_tables(args.output_dir)
    end_time = time.time()
    logger.info(
        "Data generated successfully",
        output_format=args.output_format,
        output_dir=args.output_dir,
        duration_s=end_time - start_time,
    )


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

import argparse
import time
from dataclasses import dataclass
from typing import Any, Dict

from loguru import logger

from application_sdk.test_utils.scale_data_generator.test_on_source.config_loader import (
    ConfigLoader,
)
from application_sdk.test_utils.scale_data_generator.test_on_source.data_cleaner import (
    DataCleaner,
)
from application_sdk.test_utils.scale_data_generator.test_on_source.data_generator import (
    DataGenerator,
)


@dataclass
class DriverArgs:
    config_path: str
    db_name: str
    source_type: str
    username: str
    password: str
    host: str
    port: str
    clean: bool = False


def get_connection_params(args: DriverArgs) -> Dict[str, Any]:
    """Convert driver arguments to connection parameters."""
    return {
        "username": args.username,
        "password": args.password,
        "host": args.host,
        "port": args.port,
    }


def driver(args: DriverArgs):
    """Main driver function to generate or clean data on source database."""
    try:
        start_time = time.time()
        config_loader = ConfigLoader(args.config_path)
        connection_params = get_connection_params(args)

        if args.clean:
            # Clean existing data
            cleaner = DataCleaner(
                config_loader=config_loader,
                db_name=args.db_name,
                source_type=args.source_type,
                connection_params=connection_params,
            )
            cleaner.clean_data()
            end_time = time.time()
            logger.info(
                f"Data cleaned successfully. Time taken: {end_time - start_time} seconds"
            )
        else:
            # Generate new data
            generator = DataGenerator(
                config_loader=config_loader,
                db_name=args.db_name,
                source_type=args.source_type,
                connection_params=connection_params,
            )
            generator.generate_data()
            end_time = time.time()
            logger.info(
                f"Data generated successfully. Time taken: {end_time - start_time} seconds"
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate or clean scaled test data on source database"
    )
    parser.add_argument(
        "--config-path",
        help="Path to the YAML configuration file",
        default="application_sdk/test_utils/scale_data_generator/test_on_source/examples/config.yaml",
    )
    parser.add_argument(
        "--db-name",
        help="Name of the database to connect to",
        default="postgres",
    )
    parser.add_argument(
        "--source-type",
        choices=["postgresql", "mysql"],
        help="Type of source database",
        default="postgresql",
    )
    parser.add_argument(
        "--username",
        help="Database username",
        default="postgres",
    )
    parser.add_argument(
        "--password",
        help="Database password",
        default="postgres",
    )
    parser.add_argument(
        "--host",
        help="Database host",
        default="localhost",
    )
    parser.add_argument(
        "--port",
        help="Database port",
        default="5432",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean existing data instead of generating new data",
    )

    args = parser.parse_args()
    driver(DriverArgs(**vars(args)))

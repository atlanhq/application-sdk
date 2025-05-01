import argparse
import time
from dataclasses import dataclass
from typing import Optional, Type

from loguru import logger

from application_sdk.test_utils.scale_data_generator.test_containers.data_generator import (
    DataGenerator,
)
from application_sdk.test_utils.scale_data_generator.test_on_source.config_loader import (
    ConfigLoader,
)


@dataclass
class DriverArgs:
    config_path: str
    source_type: str
    container_class: Optional[Type] = None


def driver(args: DriverArgs):
    """Main driver function to generate data in a test container."""
    try:
        start_time = time.time()
        config_loader = ConfigLoader(args.config_path)

        # Generate data in container
        generator = DataGenerator(
            config_loader=config_loader,
            source_type=args.source_type,
            container_class=args.container_class,
        )

        try:
            # Generate data and get connection parameters
            connection_params = generator.generate_data()

            # Log connection details
            logger.info(
                f"Container started successfully with following connection details:\n"
                f"Host: {connection_params['host']}\n"
                f"Port: {connection_params['port']}\n"
                f"Database: {connection_params['db_name']}\n"
                f"Username: {connection_params['username']}\n"
                f"Password: {connection_params['password']}"
            )

            # Keep the container running until interrupted
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Received interrupt signal, stopping container...")

        finally:
            # Always stop the container
            generator.stop_container()

        end_time = time.time()
        logger.info(f"Container stopped. Total time: {end_time - start_time} seconds")

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate scaled test data in a test container"
    )
    parser.add_argument(
        "--config-path",
        help="Path to the YAML configuration file",
        default="application_sdk/test_utils/scale_data_generator/test_containers/examples/config.yaml",
    )
    parser.add_argument(
        "--source-type",
        choices=["postgresql", "mysql"],
        help="Type of database to use in container",
        default="postgresql",
    )

    args = parser.parse_args()
    driver(DriverArgs(**vars(args)))

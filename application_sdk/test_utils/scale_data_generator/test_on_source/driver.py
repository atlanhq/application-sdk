import time
from dataclasses import dataclass

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
    connection_url: str
    config_path: str
    clean: bool = False


def get_connection_url(args: DriverArgs) -> str:
    """Construct database connection URL from arguments."""
    return args.connection_url


def driver(args: DriverArgs):
    """Main driver function to generate or clean data on source database."""
    try:
        start_time = time.time()
        config_loader = ConfigLoader(args.config_path)
        connection_url = get_connection_url(args)

        if args.clean:
            # Clean existing data
            cleaner = DataCleaner(
                config_loader=config_loader,
                connection_url=connection_url,
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
                connection_url=connection_url,
            )
            generator.generate_data()
            end_time = time.time()
            logger.info(
                f"Data generated successfully. Time taken: {end_time - start_time} seconds"
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from typing import Any, Dict, List, Tuple
import uuid

import aiofiles
import psycopg2
from sqlalchemy import Connection

from application_sdk.common.converter import transform_metadata
from application_sdk.common.schema import PydanticJSONEncoder
from application_sdk.dto.credentials import BasicCredential

logger = logging.getLogger(__name__)


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str, temp_table_regex_str: str
) -> Tuple[str, str, str]:
    """
    Prepare the filters for the SQL query.

    Args:
        include_filter_str (str): The include filter string.
        exclude_filter_str (str): The exclude filter string.
        temp_table_regex_str (str): The temporary table regex string.

    Returns:
        Tuple[str, str, str]: The normalized include regex, the normalized exclude regex, and the exclude table.
    """
    include_filter = json.loads(include_filter_str)
    exclude_filter = json.loads(exclude_filter_str)

    normalized_include_filter_list = normalize_filters(include_filter, True)
    normalized_exclude_filter_list = normalize_filters(exclude_filter, False)

    normalized_include_regex = (
        "|".join(normalized_include_filter_list)
        if normalized_include_filter_list
        else ".*"
    )
    normalized_exclude_regex = (
        "|".join(normalized_exclude_filter_list)
        if normalized_exclude_filter_list
        else "$^"
    )

    exclude_table = temp_table_regex_str if temp_table_regex_str else "$^"

    return normalized_include_regex, normalized_exclude_regex, exclude_table


def normalize_filters(filter_dict: Dict[str, List[str]], is_include: bool) -> List[str]:
    """
    Normalize the filters for the SQL query.

    Args:
        filter_dict (Dict[str, List[str]]): The filter dictionary.
        is_include (bool): Whether the filter is an include filter.

    Returns:
        List[str]: The normalized filter list.
    """
    normalized_filter_list: List[str] = []
    for filtered_db, filtered_schemas in filter_dict.items():
        db = filtered_db.strip("^$")
        if not filtered_schemas:
            normalized_filter_list.append(f"{db}.*")
        else:
            for schema in filtered_schemas:
                sch = schema.lstrip(
                    "^"
                )  # we do not strip out the $ as it is used to match the end of the string
                normalized_filter_list.append(f"{db}.{sch}")

    return normalized_filter_list


def get_connection(credential: Dict[str, Any]):
    # FIXME: this has to be a generic driver connect rather than pg based
    return psycopg2.connect(**credential)


async def run_query_in_batch(connection: Connection, query: str, batch_size: int = 100000):
    """
    Run a query in a batch mode with server-side cursor.
    """
    loop = asyncio.get_running_loop()

    with ThreadPoolExecutor() as pool:
        # Use a unique name for the server-side cursor
        cursor_name = f"cursor_{uuid.uuid4()}"
        cursor = await loop.run_in_executor(
            pool, lambda: connection.cursor(name=cursor_name)
        )

        try:
            # Execute the query
            await loop.run_in_executor(pool, cursor.execute, query)
            column_names: List[str] = []

            while True:
                rows = await loop.run_in_executor(
                    pool, cursor.fetchmany, batch_size
                )
                if not column_names:
                    column_names = [desc[0] for desc in cursor.description]

                if not rows:
                    break

                results = [dict(zip(column_names, row)) for row in rows]
                yield results
        except Exception as e:
            logger.error(f"Error running query in batch: {e}")
            raise e
        finally:
            await loop.run_in_executor(pool, cursor.close)

    logger.info(f"Query execution completed")



async def process_batch(
        results: List[Dict[str, Any]],
        typename: str,
        output_path: str,
        summary: Dict[str, int],
        chunk_number: int,
    ) -> None:
        raw_batch: List[str] = []
        transformed_batch: List[str] = []

        for row in results:
            try:
                raw_batch.append(json.dumps(row))
                summary["raw"] += 1

                transformed_data = transform_metadata(
                    "CONNECTOR_NAME", "CONNECTOR_TYPE", typename, row
                )
                if transformed_data is not None:
                    transformed_batch.append(
                        json.dumps(
                            transformed_data.model_dump(), cls=PydanticJSONEncoder
                        )
                    )
                    summary["transformed"] += 1
                else:
                    logger.warning(f"Skipped invalid {typename} data: {row}")
                    summary["errored"] += 1
            except Exception as row_error:
                logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )
                summary["errored"] += 1

        # Write batches to files
        raw_file = os.path.join(output_path, "raw", f"{typename}-{chunk_number}.json")
        transformed_file = os.path.join(
            output_path, "transformed", f"{typename}-{chunk_number}.json"
        )

        async with aiofiles.open(raw_file, "a") as raw_f:
            await raw_f.write("\n".join(raw_batch) + "\n")

        async with aiofiles.open(transformed_file, "a") as trans_f:
            await trans_f.write("\n".join(transformed_batch) + "\n")


async def fetch_and_process_data(credentials: BasicCredential, output_path: str, sql_query: str, typename: str) -> Dict[str, Any]:
    connection = None
    summary = {"raw": 0, "transformed": 0, "errored": 0}
    chunk_number = 0

    try:
        connection = get_connection(credentials.model_dump())
        async for batch in run_query_in_batch(connection, sql_query):
            # Process each batch here
            await process_batch(batch, typename, output_path, summary, chunk_number)
            chunk_number += 1

        chunk_meta_file = os.path.join(output_path, f"{typename}-chunks.txt")
        async with aiofiles.open(chunk_meta_file, "w") as chunk_meta_f:
            await chunk_meta_f.write(str(chunk_number))
    except Exception as e:
        logger.error(f"Error fetching databases: {e}")
        raise e
    finally:
        if connection:
            connection.close()

    return {typename: summary}

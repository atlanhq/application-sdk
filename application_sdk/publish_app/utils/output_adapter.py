"""Utilities for adapting output to the required format."""

import json
import os
import time
from typing import Any, Dict, List

import daft

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


class OutputAdapter:
    """Adapter for converting processing results to the required output format."""

    @staticmethod
    async def convert_to_output_format(
        records: List[Dict[str, Any]], results: Dict[str, Any], output_dir: str
    ) -> str:
        """Convert processed records to output format and save as parquet.

        Args:
            records: Original input records
            results: Processing results containing status information
            output_dir: Directory to store output files

        Returns:
            Path to the output file
        """
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Map result details by qualified name
        detail_map = {}
        for detail in results.get("details", []):
            qualified_name = detail.get("qualified_name")
            if qualified_name:
                detail_map[qualified_name] = detail

        # Prepare output records
        output_records = []

        for record in records:
            qualified_name = record.get("qualified_name")
            if not qualified_name:
                logger.warning(f"Record missing qualified_name: {record}")
                continue

            # Get result detail for this record
            detail = detail_map.get(
                qualified_name,
                {
                    "status": "UNKNOWN",
                    "error_code": None,
                    "atlas_error_code": None,
                    "error_description": "No status information available",
                    "request_config": {
                        "api_endpoint": "unknown",
                        "method": "unknown",
                        "retry_count": 0,
                    },
                    "runbook_link": None,
                },
            )

            # Copy original record
            output_record = record.copy()

            # Add status fields
            output_record["status"] = detail.get("status", "UNKNOWN")
            output_record["error_code"] = detail.get("error_code")
            output_record["atlas_error_code"] = detail.get("atlas_error_code")
            output_record["error_description"] = detail.get("error_description")
            output_record["request_config"] = json.dumps(
                detail.get("request_config", {})
            )
            output_record["runbook_link"] = detail.get("runbook_link")

            output_records.append(output_record)

        # Generate output file path
        timestamp = int(time.time())
        asset_type = records[0].get("type_name", "unknown") if records else "unknown"
        diff_status = records[0].get("diff_status", "UNKNOWN") if records else "UNKNOWN"

        output_filename = f"{asset_type}_{diff_status}_{timestamp}.parquet"
        output_path = os.path.join(output_dir, output_filename)

        # Convert to Daft dataframe and write to parquet
        if output_records:
            # Convert to dataframe
            df = daft.from_pylist(output_records)

            # Write to parquet
            df.write_parquet(output_path)

            logger.info(f"Wrote {len(output_records)} records to {output_path}")
        else:
            logger.warning("No records to write to output file")

        return output_path

    @staticmethod
    async def combine_files(
        file_paths: List[str], output_dir: str, asset_type: str, diff_status: str
    ) -> str:
        """Combine multiple parquet files into a single file.

        Args:
            file_paths: List of files to combine
            output_dir: Directory to store the combined file
            asset_type: Asset type
            diff_status: Diff status

        Returns:
            Path to the combined file
        """
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        if not file_paths:
            logger.warning("No files to combine")
            return ""

        # Generate output file path
        timestamp = int(time.time())
        output_filename = f"{asset_type}_{diff_status}_{timestamp}_combined.parquet"
        output_path = os.path.join(output_dir, output_filename)

        try:
            # Read and combine all files
            df = daft.read_parquet(file_paths)

            # Write combined file
            df.write_parquet(output_path)

            logger.info(f"Combined {len(file_paths)} files into {output_path}")

            return output_path

        except Exception as e:
            logger.error(f"Failed to combine files: {str(e)}")
            raise

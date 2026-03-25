"""Pandera-based data validation for integration testing.

This module provides schema-based validation of extracted output files
using pandera YAML schemas. It validates that the actual data produced
by a workflow conforms to expected column types, value ranges, and
record counts.

This is separate from `comparison.py` (which diffs metadata assets) —
pandera validates column-level schema on raw output files.

Usage:
    Define pandera schemas as YAML files in a directory structure
    that mirrors the extracted output structure:

        tests/integration/schema/
            Database/schema.yaml
            Table/schema.yaml
            Column/schema.yaml

    Then set `schema_base_path` on the scenario or test class:

        >>> Scenario(
        ...     name="workflow_with_validation",
        ...     api="workflow",
        ...     schema_base_path="tests/integration/schema",
        ...     assert_that={"success": equals(True)},
        ... )
"""

import os
from glob import glob
from typing import Any, Dict, List

import orjson
import pandas as pd
import pandera.extensions as extensions
from pandera.io import from_yaml

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# =============================================================================
# Custom Pandera Check Methods
# =============================================================================


@extensions.register_check_method(statistics=["expected_record_count"])
def check_record_count_ge(df: pd.DataFrame, *, expected_record_count: int) -> bool:
    """Validate that a DataFrame has at least the expected number of records.

    This is registered as a custom pandera check method that can be used
    in YAML schema files:

        checks:
          check_record_count_ge:
            expected_record_count: 10

    Args:
        df: The DataFrame to validate.
        expected_record_count: Minimum expected row count.

    Returns:
        bool: True if the record count is sufficient.

    Raises:
        ValueError: If the record count is below the expected minimum.
    """
    if df.shape[0] >= expected_record_count:
        return True
    raise ValueError(
        f"Expected record count >= {expected_record_count}, got: {df.shape[0]}"
    )


# =============================================================================
# Data Loading
# =============================================================================


def get_normalised_dataframe(extracted_file_path: str) -> pd.DataFrame:
    """Read extracted output files and normalize into a DataFrame.

    Supports JSON (line-delimited) and Parquet files. All files in the
    directory tree are merged into a single DataFrame.

    Args:
        extracted_file_path: Directory containing extracted output files.

    Returns:
        pd.DataFrame: Normalized DataFrame with all records.

    Raises:
        FileNotFoundError: If no data files are found.
    """
    data: List[Dict[str, Any]] = []

    # Search for JSON and parquet files
    json_files = glob(f"{extracted_file_path}/**/*.json", recursive=True)
    parquet_files = glob(f"{extracted_file_path}/**/*.parquet", recursive=True)
    files_list = json_files or parquet_files

    for f_name in files_list or []:
        if f_name.endswith(".parquet"):
            df = pd.read_parquet(f_name)
            data.extend(df.to_dict(orient="records"))
        elif f_name.endswith(".json"):
            with open(f_name, "rb") as f:
                data.extend([orjson.loads(line) for line in f])

    if not data:
        raise FileNotFoundError(
            f"No data found in extracted directory: {extracted_file_path}"
        )

    return pd.json_normalize(data)


def get_schema_file_paths(schema_base_path: str) -> List[str]:
    """Find all pandera YAML schema files in the given directory.

    Recursively searches for .yaml and .yml files.

    Args:
        schema_base_path: Root directory containing schema files.

    Returns:
        List[str]: Sorted list of schema file paths.

    Raises:
        FileNotFoundError: If no schema files are found.
    """
    search_pattern = f"{schema_base_path}/**/*"
    yaml_files = glob(f"{search_pattern}.yaml", recursive=True) + glob(
        f"{search_pattern}.yml", recursive=True
    )

    if not yaml_files:
        raise FileNotFoundError(f"No pandera schema files found in: {schema_base_path}")

    return sorted(yaml_files)


# =============================================================================
# Validation
# =============================================================================


def validate_with_pandera(
    schema_base_path: str,
    extracted_output_path: str,
    subdirectory: str = "transformed",
) -> List[Dict[str, Any]]:
    """Validate extracted output against pandera YAML schemas.

    For each schema file found under `schema_base_path`, the validator:
    1. Derives the corresponding extracted output subdirectory
    2. Loads and normalizes the extracted data into a DataFrame
    3. Validates the DataFrame against the pandera schema

    The schema file path relative to `schema_base_path` determines which
    subdirectory of extracted output to validate. For example:

        Schema: tests/integration/schema/Database/schema.yaml
        Output: {extracted_output_path}/{subdirectory}/Database/

    Args:
        schema_base_path: Root directory containing pandera YAML schemas.
        extracted_output_path: Root directory of extracted workflow output
            (typically: base_path/workflow_id/run_id).
        subdirectory: Subdirectory within output path to search.
            Defaults to "transformed".

    Returns:
        List of validation result dicts, each containing:
            - schema_file: Path to the schema file
            - entity: The entity type (e.g., "Database", "Table")
            - success: Whether validation passed
            - error: Error message if validation failed (None if passed)
            - record_count: Number of records validated

    Raises:
        FileNotFoundError: If schema_base_path doesn't exist or has no schemas.
    """
    if not os.path.exists(schema_base_path):
        raise FileNotFoundError(f"Schema base path not found: {schema_base_path}")

    schema_files = get_schema_file_paths(schema_base_path)
    results: List[Dict[str, Any]] = []

    output_base = (
        os.path.join(extracted_output_path, subdirectory)
        if subdirectory
        else extracted_output_path
    )

    for schema_file in schema_files:
        # Derive the entity type from the schema file path
        # e.g., "tests/schema/Database/schema.yaml" -> "Database"
        relative_path = schema_file.replace(schema_base_path, "")
        entity = (
            relative_path.replace(".yaml", "")
            .replace(".yml", "")
            .strip(os.sep)
            .split(os.sep)[0]
            if os.sep in relative_path.strip(os.sep)
            else os.path.splitext(os.path.basename(schema_file))[0]
        )

        # Derive the extracted data path
        # Schema path relative to base determines output subdirectory
        extracted_path_suffix = (
            relative_path.replace(".yaml", "").replace(".yml", "").strip(os.sep)
        )
        # Remove the schema filename part (e.g., "Database/schema" -> "Database")
        if os.sep in extracted_path_suffix:
            extracted_path_suffix = os.path.dirname(extracted_path_suffix)

        extracted_file_path = os.path.join(output_base, extracted_path_suffix)

        result: Dict[str, Any] = {
            "schema_file": schema_file,
            "entity": entity,
            "success": False,
            "error": None,
            "record_count": 0,
        }

        try:
            logger.info(f"Validating {entity} data against {schema_file}")

            # Load pandera schema from YAML
            schema = from_yaml(schema_file)

            # Load and normalize extracted data
            dataframe = get_normalised_dataframe(extracted_file_path)
            result["record_count"] = len(dataframe)

            # Validate with lazy error reporting
            schema.validate(dataframe, lazy=True)

            result["success"] = True
            logger.info(
                f"Validation passed for {entity}: "
                f"{result['record_count']} records validated"
            )

        except FileNotFoundError as e:
            result["error"] = str(e)
            logger.warning(f"Skipping {entity} validation: {e}")
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"Validation failed for {entity}: {e}")

        results.append(result)

    return results


def format_validation_report(results: List[Dict[str, Any]]) -> str:
    """Format pandera validation results into a human-readable report.

    Args:
        results: List of validation result dicts from validate_with_pandera.

    Returns:
        str: Formatted report string.
    """
    lines = ["Pandera Data Validation Report:", ""]
    passed = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    total = len(results)

    lines.append(f"Summary: {passed}/{total} passed, {failed} failed")
    lines.append("")

    for result in results:
        status = "PASS" if result["success"] else "FAIL"
        line = f"  [{status}] {result['entity']} ({result['record_count']} records)"
        if result["error"]:
            # Truncate long error messages
            error_preview = result["error"][:200]
            if len(result["error"]) > 200:
                error_preview += "..."
            line += f"\n         Error: {error_preview}"
        lines.append(line)

    return "\n".join(lines)

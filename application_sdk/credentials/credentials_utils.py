"""Utilities for credential providers."""

import collections.abc
import json
from typing import Any, Dict

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


def process_secret_data(secret_data: Any) -> Dict[str, Any]:
    """
    Process raw secret data into a standardized dictionary format.

    Args:
        secret_data (Any): Raw secret data from various sources.

    Returns:
        Dict[str, Any]: Processed secret data as a dictionary.
    """
    # Convert ScalarMapContainer to dict if needed
    if isinstance(secret_data, collections.abc.Mapping):
        secret_data = dict(secret_data)

    # If the dict has a single key and its value is a JSON string, parse it
    if len(secret_data) == 1 and isinstance(next(iter(secret_data.values())), str):
        try:
            parsed = json.loads(next(iter(secret_data.values())))
            if isinstance(parsed, dict):
                secret_data = parsed
        except Exception:
            pass

    return secret_data


def apply_secret_values(
    source_credentials: Dict[str, Any], secret_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Apply secret values to source credentials by substituting references.

    This function replaces values in the source credentials with values
    from the secret data when the source value exists as a key in the secrets.

    Args:
        source_credentials (Dict[str, Any]): Original credentials with potential references.
        secret_data (Dict[str, Any]): Secret data containing actual values.

    Returns:
        Dict[str, Any]: Credentials with secret values applied.
    """
    result_credentials = source_credentials.copy()

    # Replace credential values with secret values
    for key, value in list(result_credentials.items()):
        if isinstance(value, str) and value in secret_data:
            result_credentials[key] = secret_data[value]

    # Apply the same substitution to the 'extra' dictionary
    if "extra" in result_credentials and isinstance(result_credentials["extra"], dict):
        for key, value in list(result_credentials["extra"].items()):
            if isinstance(value, str) and value in secret_data:
                result_credentials["extra"][key] = secret_data[value]

    return result_credentials

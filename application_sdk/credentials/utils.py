"""Credential parsing utilities."""

import json
from typing import Any, Dict, Union

from application_sdk.common.error_codes import CommonError


def parse_credentials_extra(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the 'extra' field from credentials, handling both string and dict inputs.

    Args:
        credentials: Credentials dictionary containing an 'extra' field.

    Returns:
        Parsed extra field as a dictionary.

    Raises:
        CommonError: If the extra field contains invalid JSON.
    """
    extra: Union[str, Dict[str, Any]] = credentials.get("extra", {})

    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError as e:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid JSON in credentials extra field: {e}"
            ) from e

    return extra

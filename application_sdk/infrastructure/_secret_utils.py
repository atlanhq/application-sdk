"""Shared secret-data processing utilities for the infrastructure layer.

Extracted from ``_dapr/client.py`` and ``secrets.py`` to eliminate duplication
without creating a circular import between those two modules.
"""

import collections.abc
import json
from typing import Any


def process_secret_data(secret_data: Any) -> dict[str, Any]:
    """Process raw Dapr secret data into a standardised dictionary.

    Converts ``ScalarMapContainer`` (and other ``Mapping``-like types returned
    by the Dapr gRPC client) to a plain ``dict``, then unwraps single-key
    secrets that contain a JSON-encoded dict.

    Args:
        secret_data: The raw ``.secret`` value from a Dapr ``get_secret``
            response.

    Returns:
        A plain ``dict[str, Any]`` ready for credential resolution.
    """
    if isinstance(secret_data, collections.abc.Mapping):
        secret_data = dict(secret_data)
    if len(secret_data) == 1:
        k, v = next(iter(secret_data.items()))
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {k: v}
    return secret_data

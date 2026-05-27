"""Internal env-var parsing helpers shared across SDK modules."""

import logging
import os

logger = logging.getLogger(__name__)


def env_int(key: str, default: int) -> int:
    """Read an int env var, returning ``default`` when unset, empty, or unparsable.

    A malformed value like ``ATLAN_HANDLER_PORT="not-a-number"`` falls through
    to the default instead of crashing startup.
    """
    val = os.environ.get(key)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning(
            "Ignoring non-integer env var %s=%r; falling back to default %d",
            key,
            val,
            default,
        )
        return default

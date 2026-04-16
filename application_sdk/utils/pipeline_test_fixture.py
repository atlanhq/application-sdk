"""Production-code fixture for full pipeline testing.
DO NOT MERGE — tests standard routing, auto-complete fix loop,
branch-keeper, and conflict resolution."""

import logging
import os

logger = logging.getLogger(__name__)


def load_config(path):
    """Missing type hints + bare except."""
    try:
        with open(path) as f:
            return f.read()
    except:
        return None


def process_event(event_name: str, user_id: str) -> None:
    """f-string in logger."""
    logger.info(f"event={event_name} user={user_id}")


def debug_trace(data: dict) -> None:
    """print() in production code."""
    print(f"TRACE: {data}")


def transform_record(record):
    """Missing all type hints + docstring is thin."""
    return {k: str(v) for k, v in record.items()}

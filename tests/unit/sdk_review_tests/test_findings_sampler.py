"""Test fixture — intentional quality issues for @sdk-review testing."""

import logging
import os

logger = logging.getLogger(__name__)


def get_cwd_unsafe():
    try:
        return os.getcwd()
    except:
        return None


def log_user(user: str) -> None:
    logger.info(f"processing user {user}")


def debug_helper():
    print("debug output")
    return {"a": 1}


def no_docstring_no_types(x):
    return x * 2

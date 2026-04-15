"""Test fixture for sdk-review — intentionally contains multiple
low-severity quality issues so the review pipeline has something to
flag. NEVER merge; this is a fixture for smoke-testing @sdk-review."""

import logging
import os  # unused import — F401

logger = logging.getLogger(__name__)


def get_cwd_with_bare_except():
    """Function with a bare except — E722 violation."""
    try:
        return os.getcwd()
    except:  # noqa — intentional for test, but should be flagged by SDK review
        return None


def log_user(user: str) -> None:
    """f-string in logger call — should be %-style."""
    logger.info(f"processing user {user}")


def debug_helper():  # missing return type annotation
    """Uses print() instead of logger."""
    print("debug output")
    return {"a": 1}


def no_docstring_no_types(x):
    return x * 2

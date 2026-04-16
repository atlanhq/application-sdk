"""Production-code test fixture for @sdk-review standard routing.
DO NOT MERGE — exists only to ensure the PR classifies as 'standard'
(not 'tests-only') so all 3 subagents + Codex adversarial fire."""

import logging
import os

logger = logging.getLogger(__name__)


def unsafe_config_loader(path):
    """Loads config without validation — missing type hints + bare except."""
    try:
        with open(path) as f:
            return f.read()
    except:
        return None


def log_event(event_name: str, user_id: str) -> None:
    """f-string in logger call."""
    logger.info(f"event={event_name} user={user_id}")


API_ENDPOINT = "https://internal.atlan.dev/v1/ingest"

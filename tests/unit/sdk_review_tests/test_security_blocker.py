"""Test fixture for sdk-review — contains intentional CRITICAL security
issues so the review pipeline produces a BLOCKED verdict. NEVER merge."""

import logging

logger = logging.getLogger(__name__)

# CRITICAL: hardcoded secret — should be rejected by guardrail G5
API_KEY = "sk-proj-test1234567890abcdefghijklmnopqrstuv"
DB_PASSWORD = "super_secret_password_123"  # CRITICAL: hardcoded credential


def get_user_by_name(user_input: str, db_conn) -> dict:
    """SQL injection vulnerability — user input concatenated into query.

    Should be flagged as CRITICAL [SEC] — SQL injection (G1).
    """
    # CRITICAL: SQL injection
    query = f"SELECT * FROM users WHERE name = '{user_input}'"
    cursor = db_conn.execute(query)
    return cursor.fetchone()


def log_auth_header(headers: dict) -> None:
    """Logs the Authorization header in plain text — CRITICAL leak.

    Should be flagged as CRITICAL [SEC] — credential in logs.
    """
    logger.info("auth header: %s", headers.get("Authorization"))


def fetch_from_user_url(url: str):
    """No host allowlist — SSRF risk.

    Should be flagged as HIGH/CRITICAL [SEC] — SSRF.
    """
    import requests
    return requests.get(url, timeout=5)

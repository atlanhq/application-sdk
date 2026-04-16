"""Test fixture — intentional CRITICAL security issues for @sdk-review testing."""

import logging

logger = logging.getLogger(__name__)

API_KEY = "sk-proj-test1234567890abcdefghijklmnopqrstuv"
DB_PASSWORD = "super_secret_password_123"


def get_user_by_name(user_input: str, db_conn) -> dict:
    query = f"SELECT * FROM users WHERE name = '{user_input}'"
    cursor = db_conn.execute(query)
    return cursor.fetchone()


def log_auth_header(headers: dict) -> None:
    logger.info("auth header: %s", headers.get("Authorization"))

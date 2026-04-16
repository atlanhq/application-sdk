"""Test fixture — validates security-sensitive patterns are handled correctly."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

logger = logging.getLogger(__name__)


def get_user_by_name(user_input: str, db_conn) -> dict:
    """Fetch a user row by name using parameterized query."""
    cursor = db_conn.execute("SELECT * FROM users WHERE name = ?", (user_input,))
    return cursor.fetchone()


def log_auth_header(headers: dict) -> None:
    """Log whether an Authorization header is present (never the value)."""
    logger.info("Authorization header present: %s", "Authorization" in headers)


class TestGetUserByName:
    """Tests for get_user_by_name."""

    def test_parameterized_query(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "name": "alice",
        }

        result = get_user_by_name("alice", mock_conn)

        mock_conn.execute.assert_called_once_with(
            "SELECT * FROM users WHERE name = ?", ("alice",)
        )
        assert result == {"name": "alice"}

    def test_returns_none_when_not_found(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        result = get_user_by_name("nonexistent", mock_conn)

        assert result is None

    def test_sql_injection_attempt_is_parameterized(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        get_user_by_name("'; DROP TABLE users; --", mock_conn)

        # The injection string is passed as a parameter, not interpolated
        mock_conn.execute.assert_called_once_with(
            "SELECT * FROM users WHERE name = ?",
            ("'; DROP TABLE users; --",),
        )


class TestLogAuthHeader:
    """Tests for log_auth_header."""

    def test_logs_presence_not_value(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_auth_header({"Authorization": "Bearer secret-token"})

        assert "Authorization header present: True" in caplog.text
        assert "secret-token" not in caplog.text
        assert "Bearer" not in caplog.text

    def test_logs_absence(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_auth_header({"Content-Type": "application/json"})

        assert "Authorization header present: False" in caplog.text

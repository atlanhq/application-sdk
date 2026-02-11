import socket
from unittest.mock import patch

from application_sdk.test_utils.e2e.fixtures.readiness import check_tcp


class TestCheckTcp:
    def test_returns_true_when_connection_succeeds(self):
        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.__exit__ = lambda s, *a: None
            assert check_tcp("localhost", 5432) is True

    def test_returns_false_on_connection_refused(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            assert check_tcp("localhost", 5432) is False

    def test_returns_false_on_os_error(self):
        with patch("socket.create_connection", side_effect=OSError):
            assert check_tcp("localhost", 5432) is False

    def test_returns_false_on_timeout(self):
        with patch("socket.create_connection", side_effect=socket.timeout):
            assert check_tcp("localhost", 5432) is False

    def test_custom_timeout_passed(self):
        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.__exit__ = lambda s, *a: None
            check_tcp("localhost", 5432, timeout=10.0)
            mock_conn.assert_called_once_with(("localhost", 5432), timeout=10.0)

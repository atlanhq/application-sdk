import socket
from unittest.mock import MagicMock, patch

from application_sdk.test_utils.e2e.fixtures.readiness import check_tcp


def _make_mock_socket(recv_side_effect=None):
    """Create a mock socket that supports the context manager and recv."""
    mock_sock = MagicMock()
    if recv_side_effect is not None:
        mock_sock.recv.side_effect = recv_side_effect
    else:
        # Default: recv times out (service holds connection open)
        mock_sock.recv.side_effect = socket.timeout
    return mock_sock


class TestCheckTcp:
    def test_returns_true_when_service_holds_connection(self):
        mock_sock = _make_mock_socket(recv_side_effect=socket.timeout)
        with patch("socket.create_connection", return_value=mock_sock):
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = lambda s, *a: None
            assert check_tcp("localhost", 5432) is True

    def test_returns_true_when_service_sends_data(self):
        mock_sock = _make_mock_socket(recv_side_effect=[b"\x00"])
        with patch("socket.create_connection", return_value=mock_sock):
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = lambda s, *a: None
            assert check_tcp("localhost", 5432) is True

    def test_returns_false_when_connection_immediately_closed(self):
        mock_sock = _make_mock_socket(
            recv_side_effect=[b""]
        )  # empty = connection closed
        with patch("socket.create_connection", return_value=mock_sock):
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = lambda s, *a: None
            assert check_tcp("localhost", 5432) is False

    def test_returns_false_when_connection_reset(self):
        mock_sock = _make_mock_socket(recv_side_effect=ConnectionResetError)
        with patch("socket.create_connection", return_value=mock_sock):
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = lambda s, *a: None
            assert check_tcp("localhost", 5432) is False

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
        mock_sock = _make_mock_socket(recv_side_effect=socket.timeout)
        with patch("socket.create_connection", return_value=mock_sock) as mock_conn:
            mock_sock.__enter__ = lambda s: s
            mock_sock.__exit__ = lambda s, *a: None
            check_tcp("localhost", 5432, timeout=10.0)
            mock_conn.assert_called_once_with(("localhost", 5432), timeout=10.0)

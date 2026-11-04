"""_check_server_health reports an unreachable or unusable server as unhealthy."""

from unittest.mock import patch

import httpx
import pytest

from application_sdk.testing.integration.runner import _check_server_health


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
def test_transport_errors_are_unhealthy(error: Exception) -> None:
    with patch("httpx.get", side_effect=error):
        assert _check_server_health("http://localhost:8000") is False


def test_malformed_url_is_unhealthy_not_raised() -> None:
    # httpx.InvalidURL is not an httpx.HTTPError; it is raised before any
    # connection, so no socket is opened.
    assert _check_server_health("http://localhost:not-a-port") is False


def test_200_is_healthy() -> None:
    with patch("httpx.get", return_value=httpx.Response(200)) as get:
        assert _check_server_health("http://app") is True
    assert get.call_args.args[0] == "http://app/server/health"

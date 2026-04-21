"""Unit tests for the provision_credentials CLI utility."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestProvisionCredentialsBodyFlag:
    """Tests for --body flag parsing."""

    def test_body_flag_parses_json_string(self) -> None:
        """--body '{"host": "localhost"}' sends correct JSON to the endpoint."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"credential_guid": "abc123def456"},
        }
        mock_response.raise_for_status = MagicMock()

        with (
            patch("requests.post", return_value=mock_response) as mock_post,
            patch(
                "sys.argv",
                [
                    "provision-credentials",
                    "--body",
                    '{"host": "localhost", "port": 5432}',
                ],
            ),
            patch("builtins.print"),
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"host": "localhost", "port": 5432}
        assert "/workflows/v1/dev/local-vault" in call_kwargs[0][0]

    def test_body_flag_with_nested_json(self) -> None:
        """--body with nested extra field works correctly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"credential_guid": "guid123"},
        }
        mock_response.raise_for_status = MagicMock()

        body = json.dumps(
            {
                "host": "db.example.com",
                "username": "admin",
                "password": "secret",
                "extra": {"ssl": True, "timeout": 30},
            }
        )

        with (
            patch("requests.post", return_value=mock_response) as mock_post,
            patch("sys.argv", ["provision-credentials", "--body", body]),
            patch("builtins.print"),
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        sent_body = mock_post.call_args[1]["json"]
        assert sent_body["host"] == "db.example.com"
        assert sent_body["extra"]["ssl"] is True


class TestProvisionCredentialsFromFileFlag:
    """Tests for --from-file flag."""

    def test_from_file_reads_json(self, tmp_path: Path) -> None:
        """--from-file reads JSON from a file and sends it."""
        creds_file = tmp_path / "creds.json"
        creds_data = {"host": "db.example.com", "username": "admin", "password": "pass"}
        creds_file.write_text(json.dumps(creds_data))

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"credential_guid": "file-guid-123"},
        }
        mock_response.raise_for_status = MagicMock()

        with (
            patch("requests.post", return_value=mock_response) as mock_post,
            patch(
                "sys.argv",
                ["provision-credentials", "--from-file", str(creds_file)],
            ),
            patch("builtins.print"),
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        mock_post.assert_called_once()
        sent_body = mock_post.call_args[1]["json"]
        assert sent_body == creds_data


class TestProvisionCredentialsMissingArgs:
    """Tests for missing required arguments."""

    def test_errors_when_neither_body_nor_from_file(self) -> None:
        """Errors when neither --body nor --from-file is provided."""
        with (
            patch("sys.argv", ["provision-credentials"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        # argparse exits with code 2 for missing required args
        assert exc_info.value.code == 2

    def test_errors_when_both_body_and_from_file(self, tmp_path: Path) -> None:
        """Errors when both --body and --from-file are provided (mutually exclusive)."""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("{}")

        with (
            patch(
                "sys.argv",
                [
                    "provision-credentials",
                    "--body",
                    "{}",
                    "--from-file",
                    str(creds_file),
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        assert exc_info.value.code == 2


class TestProvisionCredentialsPortFlag:
    """Tests for --port flag."""

    def test_custom_port(self) -> None:
        """--port changes the target URL."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"credential_guid": "guid"}}
        mock_response.raise_for_status = MagicMock()

        with (
            patch("requests.post", return_value=mock_response) as mock_post,
            patch(
                "sys.argv",
                ["provision-credentials", "--body", "{}", "--port", "9090"],
            ),
            patch("builtins.print"),
        ):
            from application_sdk.tools.provision_credentials import main

            main()

        url = mock_post.call_args[0][0]
        assert "localhost:9090" in url

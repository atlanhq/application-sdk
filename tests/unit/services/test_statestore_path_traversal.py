"""Security tests for path traversal protection in StateStore."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.services.statestore import (
    PathTraversalError,
    StateStore,
    StateType,
    build_state_store_path,
)


class TestPathTraversalPrevention:
    """Tests verifying path traversal attacks are blocked."""

    def test_build_state_store_path_blocks_basic_traversal(self) -> None:
        malicious_id = "../../../../../../../tmp/pwned"

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            build_state_store_path(malicious_id, StateType.WORKFLOWS)

    def test_build_state_store_path_blocks_deep_traversal(self) -> None:
        malicious_id = "../" * 50 + "etc/passwd"

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            build_state_store_path(malicious_id, StateType.WORKFLOWS)

    def test_build_state_store_path_blocks_escape_to_tmp(self) -> None:
        malicious_id = "../../../../../../../../../../tmp/malicious"

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            build_state_store_path(malicious_id, StateType.WORKFLOWS)

    def test_build_state_store_path_blocks_escape_to_etc(self) -> None:
        malicious_id = "../../../../../../../../../../etc/passwd"

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            build_state_store_path(malicious_id, StateType.WORKFLOWS)

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../" * 10 + "tmp/pwned",
            "../" * 20 + "etc/passwd",
            "../../../../../../../../../../var/www/shell.php",
            "../" * 15 + "home/user/.ssh/authorized_keys",
        ],
    )
    def test_various_escaping_payloads_blocked(self, malicious_id: str) -> None:
        with pytest.raises(PathTraversalError):
            build_state_store_path(malicious_id, StateType.WORKFLOWS)


class TestTraversalWithinBoundsAllowed:
    """Tests for traversal sequences that stay within the base directory."""

    def test_shallow_traversal_stays_in_bounds(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        shallow_id = "foo/../bar"
        path = build_state_store_path(shallow_id, StateType.WORKFLOWS)

        abs_path = os.path.abspath(path)
        base_path = os.path.abspath(TEMPORARY_PATH)
        assert abs_path.startswith(base_path)

    def test_windows_backslash_on_unix_is_literal(self) -> None:
        windows_style = "..\\..\\..\\test"
        path = build_state_store_path(windows_style, StateType.WORKFLOWS)

        assert "\\" in path or "test" in path


class TestLegitimatePathsAllowed:
    """Tests verifying legitimate IDs still work correctly."""

    def test_simple_id_allowed(self) -> None:
        path = build_state_store_path("workflow-123", StateType.WORKFLOWS)
        assert "workflow-123" in path
        assert path.endswith("config.json")

    def test_uuid_style_id_allowed(self) -> None:
        path = build_state_store_path(
            "550e8400-e29b-41d4-a716-446655440000", StateType.WORKFLOWS
        )
        assert "550e8400-e29b-41d4-a716-446655440000" in path

    def test_underscore_id_allowed(self) -> None:
        path = build_state_store_path("my_workflow_id", StateType.WORKFLOWS)
        assert "my_workflow_id" in path

    def test_credentials_type_allowed(self) -> None:
        path = build_state_store_path("db-cred-456", StateType.CREDENTIALS)
        assert "credentials" in path
        assert "db-cred-456" in path

    def test_path_stays_within_base_directory(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        path = build_state_store_path("legitimate-workflow-id", StateType.WORKFLOWS)
        abs_path = os.path.abspath(path)
        base_path = os.path.abspath(TEMPORARY_PATH)

        assert abs_path.startswith(base_path)


class TestStateStoreMethodsProtected:
    """Tests verifying StateStore methods are protected from path traversal."""

    @pytest.mark.asyncio
    async def test_save_state_object_blocks_traversal(self) -> None:
        malicious_id = "../../../../../../../tmp/pwned"

        with patch(
            "application_sdk.services.statestore.ObjectStore.get_content",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(PathTraversalError, match="path traversal detected"):
                await StateStore.save_state_object(
                    id=malicious_id,
                    value={"pwned": True},
                    type=StateType.WORKFLOWS,
                )

    @pytest.mark.asyncio
    async def test_save_state_blocks_traversal(self) -> None:
        malicious_id = "../../../../../../../tmp/pwned"

        with patch(
            "application_sdk.services.statestore.ObjectStore.get_content",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(PathTraversalError, match="path traversal detected"):
                await StateStore.save_state(
                    key="test_key",
                    value="test_value",
                    id=malicious_id,
                    type=StateType.WORKFLOWS,
                )

    @pytest.mark.asyncio
    async def test_get_state_blocks_traversal(self) -> None:
        malicious_id = "../../../../../../../etc/passwd"

        with pytest.raises(PathTraversalError, match="path traversal detected"):
            await StateStore.get_state(malicious_id, StateType.WORKFLOWS)

    @pytest.mark.asyncio
    async def test_save_state_object_allows_legitimate_id(self) -> None:
        legitimate_id = "workflow-test-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "application_sdk.services.statestore.TEMPORARY_PATH", tmpdir
            ), patch(
                "application_sdk.services.statestore.ObjectStore.get_content",
                new_callable=AsyncMock,
                return_value=None,
            ), patch(
                "application_sdk.services.statestore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ):
                result = await StateStore.save_state_object(
                    id=legitimate_id,
                    value={"status": "running"},
                    type=StateType.WORKFLOWS,
                )

                assert result == {"status": "running"}


class TestNoFileCreatedOutsideBase:
    """Tests verifying no files are created outside the base directory."""

    @pytest.mark.asyncio
    async def test_traversal_does_not_create_external_files(self) -> None:
        from application_sdk.constants import TEMPORARY_PATH

        malicious_id = "../../../../../../../tmp/should_not_exist_test"

        vulnerable_path = os.path.join(
            TEMPORARY_PATH,
            f"persistent-artifacts/apps/default/workflows/{malicious_id}/config.json",
        )
        potentially_pwned_path = os.path.abspath(vulnerable_path)
        potentially_pwned_dir = os.path.dirname(potentially_pwned_path)

        if os.path.exists(potentially_pwned_dir):
            os.rmdir(potentially_pwned_dir)

        with patch(
            "application_sdk.services.statestore.ObjectStore.get_content",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "application_sdk.services.statestore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ):
            with pytest.raises(PathTraversalError):
                await StateStore.save_state_object(
                    id=malicious_id,
                    value={"pwned": True},
                    type=StateType.WORKFLOWS,
                )

        assert not os.path.exists(potentially_pwned_path)
        assert not os.path.exists(potentially_pwned_dir)

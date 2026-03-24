"""Tests for connection normalization logic in get_workflow_args (AE/native orchestration)."""

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.handlers import HandlerInterface


class _MockHandler(HandlerInterface):
    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def load(self, config: Dict[str, Any]) -> None:
        pass

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        return True


class _MockActivities(ActivitiesInterface[ActivitiesState[_MockHandler]]):
    pass


# Common mocks for every test in this module
_PATCHES = {
    "build_output_path": "application_sdk.activities.build_output_path",
    "get_workflow_run_id": "application_sdk.activities.get_workflow_run_id",
    "get_workflow_id": "application_sdk.activities.get_workflow_id",
    "get_state": "application_sdk.services.statestore.StateStore.get_state",
}


def _base_mocks():
    """Return (mock_get_state, mock_get_workflow_id, mock_get_workflow_run_id, mock_build_output_path) patches."""
    return {
        "get_state": AsyncMock(side_effect=Exception("no state store")),
        "get_workflow_id": lambda: "wf-1",
        "get_workflow_run_id": lambda: "run-1",
        "build_output_path": lambda: "output/path",
    }


async def _call_get_workflow_args(workflow_config: Dict[str, Any]) -> Dict[str, Any]:
    mocks = _base_mocks()
    activities = _MockActivities()
    with (
        patch(_PATCHES["get_state"], mocks["get_state"]),
        patch(_PATCHES["get_workflow_id"], mocks["get_workflow_id"]),
        patch(_PATCHES["get_workflow_run_id"], mocks["get_workflow_run_id"]),
        patch(_PATCHES["build_output_path"], mocks["build_output_path"]),
    ):
        return await activities.get_workflow_args(workflow_config)


class TestConnectionNormalization:
    """Verify that get_workflow_args normalizes all three connection shapes."""

    async def test_ae_path_connection_with_attributes(self):
        """AE path: {attributes: {qualifiedName, name}, typeName: 'Connection'}"""
        config = {
            "workflow_id": "wf-1",
            "connection": {
                "typeName": "Connection",
                "attributes": {
                    "qualifiedName": "default/snowflake/123",
                    "name": "my-snowflake",
                },
            },
        }
        result = await _call_get_workflow_args(config)
        conn = result["connection"]
        assert conn["connection_qualified_name"] == "default/snowflake/123"
        assert conn["connection_name"] == "my-snowflake"

    async def test_flat_connection(self):
        """Flat shape: {qualifiedName, name} without attributes."""
        config = {
            "workflow_id": "wf-1",
            "connection": {
                "qualifiedName": "default/pg/456",
                "name": "my-pg",
            },
        }
        result = await _call_get_workflow_args(config)
        conn = result["connection"]
        assert conn["connection_qualified_name"] == "default/pg/456"
        assert conn["connection_name"] == "my-pg"

    async def test_already_normalized_connection(self):
        """SDK/state-store shape: {connection_qualified_name, connection_name}."""
        config = {
            "workflow_id": "wf-1",
            "connection": {
                "connection_qualified_name": "default/mysql/789",
                "connection_name": "my-mysql",
            },
        }
        result = await _call_get_workflow_args(config)
        conn = result["connection"]
        assert conn["connection_qualified_name"] == "default/mysql/789"
        assert conn["connection_name"] == "my-mysql"

    async def test_connection_as_json_string(self):
        """Connection provided as a JSON string (AE may serialize it)."""
        import json

        raw = {"qualifiedName": "default/bq/111", "name": "my-bq"}
        config = {
            "workflow_id": "wf-1",
            "connection": json.dumps(raw),
        }
        result = await _call_get_workflow_args(config)
        conn = result["connection"]
        assert conn["connection_qualified_name"] == "default/bq/111"
        assert conn["connection_name"] == "my-bq"

    async def test_connection_as_invalid_json_string(self):
        """Invalid JSON string falls back to empty connection."""
        config = {
            "workflow_id": "wf-1",
            "connection": "not-valid-json",
        }
        result = await _call_get_workflow_args(config)
        # Invalid string → parsed as empty dict → no normalization applied
        assert result["connection"] == "not-valid-json" or result["connection"] == {}

    async def test_connection_missing(self):
        """No connection key at all — should not crash."""
        config = {"workflow_id": "wf-1"}
        result = await _call_get_workflow_args(config)
        # Empty dict from .get("connection", {}) — no normalization applied
        assert "connection" not in result or result["connection"] == {}

    async def test_connection_empty_dict(self):
        """Empty connection dict — should not crash or add keys."""
        config = {"workflow_id": "wf-1", "connection": {}}
        result = await _call_get_workflow_args(config)
        assert result["connection"] == {}

    async def test_attributes_take_priority_over_flat_keys(self):
        """When both attributes and flat keys exist, attributes win."""
        config = {
            "workflow_id": "wf-1",
            "connection": {
                "attributes": {
                    "qualifiedName": "from-attrs",
                    "name": "attrs-name",
                },
                "qualifiedName": "from-flat",
                "name": "flat-name",
            },
        }
        result = await _call_get_workflow_args(config)
        conn = result["connection"]
        assert conn["connection_qualified_name"] == "from-attrs"
        assert conn["connection_name"] == "attrs-name"


class TestStateStoreFallback:
    """Verify that get_workflow_args falls back to workflow_config when StateStore fails."""

    async def test_fallback_to_workflow_config_on_statestore_failure(self):
        """When StateStore.get_state raises, workflow_config is used as-is."""
        config = {
            "workflow_id": "wf-1",
            "custom_key": "custom_value",
        }
        result = await _call_get_workflow_args(config)
        assert result["custom_key"] == "custom_value"
        assert result["workflow_id"] == "wf-1"

    @patch(_PATCHES["build_output_path"], return_value="output/path")
    @patch(_PATCHES["get_workflow_run_id"], return_value="run-1")
    @patch(_PATCHES["get_workflow_id"], return_value="wf-1")
    @patch(
        _PATCHES["get_state"],
        new_callable=AsyncMock,
        return_value={"workflow_id": "wf-1", "from_state": True},
    )
    async def test_statestore_success_uses_state_data(
        self, mock_get_state, mock_wf_id, mock_run_id, mock_build_path
    ):
        """When StateStore succeeds, its data is used (not the workflow_config)."""
        activities = _MockActivities()
        config = {"workflow_id": "wf-1", "from_config": True}
        result = await activities.get_workflow_args(config)

        assert result["from_state"] is True
        # from_config should NOT be in result since state store data is used
        assert "from_config" not in result


class TestAtlanPrefixedKeys:
    """Verify that atlan- prefixed keys are preserved from workflow_config."""

    async def test_atlan_prefixed_keys_preserved(self):
        """Keys starting with 'atlan-' are copied from workflow_config."""
        config = {
            "workflow_id": "wf-1",
            "atlan-tenant-id": "tenant-123",
            "atlan-request-id": "req-456",
            "normal-key": "ignored",
        }
        result = await _call_get_workflow_args(config)
        assert result["atlan-tenant-id"] == "tenant-123"
        assert result["atlan-request-id"] == "req-456"

    @patch(_PATCHES["build_output_path"], return_value="output/path")
    @patch(_PATCHES["get_workflow_run_id"], return_value="run-1")
    @patch(_PATCHES["get_workflow_id"], return_value="wf-1")
    @patch(
        _PATCHES["get_state"],
        new_callable=AsyncMock,
        return_value={"workflow_id": "wf-1", "base": "state-data"},
    )
    async def test_atlan_prefixed_keys_with_falsy_values_skipped(
        self, mock_get_state, mock_wf_id, mock_run_id, mock_build_path
    ):
        """atlan- keys with falsy values (empty string, None) are NOT preserved
        from workflow_config into state-store-sourced args."""
        config = {
            "workflow_id": "wf-1",
            "atlan-empty": "",
            "atlan-none": None,
            "atlan-valid": "value",
        }
        activities = _MockActivities()
        result = await activities.get_workflow_args(config)
        # Falsy values are skipped by the preservation loop
        assert "atlan-empty" not in result
        assert "atlan-none" not in result
        assert result["atlan-valid"] == "value"

    async def test_atlan_keys_converted_to_string(self):
        """atlan- key values are stringified."""
        config = {
            "workflow_id": "wf-1",
            "atlan-number": 42,
        }
        result = await _call_get_workflow_args(config)
        assert result["atlan-number"] == "42"

"""Tests for HandlerInterface._wrap_configmap and get_configmap (contract toolkit)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from application_sdk.handlers import HandlerInterface


class TestWrapConfigmap:
    """Tests for HandlerInterface._wrap_configmap static method."""

    def test_wrap_configmap_with_config_key(self):
        """When raw has a top-level 'config' key, that value is serialized."""
        raw = {"config": {"host": "localhost", "port": 5432}}
        result = HandlerInterface._wrap_configmap("my-config", raw)

        assert result["kind"] == "ConfigMap"
        assert result["apiVersion"] == "v1"
        assert result["metadata"]["name"] == "my-config"
        parsed_data = json.loads(result["data"]["config"])
        assert parsed_data == {"host": "localhost", "port": 5432}

    def test_wrap_configmap_without_config_key(self):
        """When raw has no 'config' key, the whole dict is serialized."""
        raw = {"host": "localhost", "port": 5432}
        result = HandlerInterface._wrap_configmap("my-config", raw)

        parsed_data = json.loads(result["data"]["config"])
        assert parsed_data == {"host": "localhost", "port": 5432}

    def test_wrap_configmap_empty_dict(self):
        """Empty raw dict produces valid ConfigMap with empty serialized object."""
        result = HandlerInterface._wrap_configmap("empty", {})

        assert result["kind"] == "ConfigMap"
        assert result["metadata"]["name"] == "empty"
        assert json.loads(result["data"]["config"]) == {}

    def test_wrap_configmap_nested_config(self):
        """Deeply nested config key is properly serialized."""
        raw = {"config": {"database": {"host": "db", "options": {"timeout": 30}}}}
        result = HandlerInterface._wrap_configmap("nested", raw)

        parsed_data = json.loads(result["data"]["config"])
        assert parsed_data["database"]["options"]["timeout"] == 30


class TestGetConfigmap:
    """Tests for HandlerInterface.get_configmap with app/generated files."""

    async def test_get_configmap_returns_empty_when_no_contract_dir(self):
        """When app/generated doesn't exist, returns empty dict."""
        with patch(
            "application_sdk.handlers.CONTRACT_GENERATED_DIR",
            Path("/nonexistent/path/app/generated"),
        ):
            result = await HandlerInterface.get_configmap("some-config")
            assert result == {}

    async def test_get_configmap_from_contract_file(self):
        """When app/generated/{id}.json exists, it's loaded and wrapped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "contract" / "generated"
            gen_dir.mkdir(parents=True)
            config_data = {"config": {"setting": "value"}}
            (gen_dir / "my-configmap.json").write_text(json.dumps(config_data))

            with patch("application_sdk.handlers.CONTRACT_GENERATED_DIR", gen_dir):
                result = await HandlerInterface.get_configmap("my-configmap")

            assert result["kind"] == "ConfigMap"
            assert result["metadata"]["name"] == "my-configmap"
            parsed_data = json.loads(result["data"]["config"])
            assert parsed_data == {"setting": "value"}

    async def test_get_configmap_no_matching_file(self):
        """When contract dir exists but has no matching file, returns empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "contract" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "other-config.json").write_text(json.dumps({"key": "val"}))

            with patch("application_sdk.handlers.CONTRACT_GENERATED_DIR", gen_dir):
                result = await HandlerInterface.get_configmap("nonexistent")

            assert result == {}

    async def test_get_configmap_skips_non_matching_files(self):
        """Only the file whose stem matches config_map_id is returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "contract" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "alpha.json").write_text(json.dumps({"config": {"a": 1}}))
            (gen_dir / "beta.json").write_text(json.dumps({"config": {"b": 2}}))

            with patch("application_sdk.handlers.CONTRACT_GENERATED_DIR", gen_dir):
                result = await HandlerInterface.get_configmap("beta")

            parsed = json.loads(result["data"]["config"])
            assert parsed == {"b": 2}

"""Tests for BaseApplication.get_manifest and BaseSQLMetadataExtractionApplication.get_manifest."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from application_sdk.application import BaseApplication
from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionWorkflow,
)


class TestBaseApplicationGetManifest:
    """Tests for BaseApplication.get_manifest."""

    def test_returns_none_when_no_contract_file(self):
        """When app/generated/manifest.json doesn't exist, returns None."""
        app = BaseApplication("test-app")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.constants.CONTRACT_GENERATED_DIR", tmpdir):
                result = app.get_manifest()
        assert result is None

    def test_returns_manifest_from_contract_file(self):
        """When app/generated/manifest.json exists, it's loaded and returned."""
        app = BaseApplication("test-app")
        manifest = {"execution_mode": "automation-engine", "dag": {"extract": {}}}

        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "app" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text(json.dumps(manifest))

            with patch(
                "application_sdk.constants.CONTRACT_GENERATED_DIR", str(gen_dir)
            ):
                result = app.get_manifest()

        assert result is not None
        assert result["execution_mode"] == "automation-engine"

    def test_deployment_name_substitution(self):
        """Manifest with {deployment_name} placeholder gets substituted."""
        app = BaseApplication("test-app")
        manifest = {"task_queue": "atlan-myapp-{deployment_name}"}

        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "app" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text(json.dumps(manifest))

            with (
                patch("application_sdk.constants.CONTRACT_GENERATED_DIR", str(gen_dir)),
                patch("application_sdk.constants.DEPLOYMENT_NAME", "my-tenant"),
            ):
                result = app.get_manifest()

        assert result["task_queue"] == "atlan-myapp-my-tenant"

    def test_app_name_substitution(self):
        """Manifest with {app_name} placeholder gets substituted."""
        app = BaseApplication("redshift")
        manifest = {
            "execution_mode": "automation-engine",
            "dag": {
                "extract": {
                    "app_name": "{app_name}",
                    "inputs": {
                        "app_name": "{app_name}",
                        "app_id": "",
                        "argo_workflow_slug": "",
                        "task_queue": "atlan-{app_name}-{deployment_name}",
                    },
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "app" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text(json.dumps(manifest))

            with (
                patch("application_sdk.constants.CONTRACT_GENERATED_DIR", str(gen_dir)),
                patch("application_sdk.constants.DEPLOYMENT_NAME", "my-tenant"),
            ):
                result = app.get_manifest()

        extract = result["dag"]["extract"]
        assert extract["app_name"] == "redshift"
        assert extract["inputs"]["app_name"] == "redshift"
        assert extract["inputs"]["app_id"] == ""
        assert extract["inputs"]["argo_workflow_slug"] == ""
        assert extract["inputs"]["task_queue"] == "atlan-redshift-my-tenant"

    def test_deployment_name_none_falls_back_to_default(self):
        """When DEPLOYMENT_NAME is None, substitution uses 'default'."""
        app = BaseApplication("test-app")
        manifest = {"task_queue": "app-{deployment_name}"}

        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "app" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text(json.dumps(manifest))

            with (
                patch("application_sdk.constants.CONTRACT_GENERATED_DIR", str(gen_dir)),
                patch("application_sdk.constants.DEPLOYMENT_NAME", None),
            ):
                result = app.get_manifest()

        assert result["task_queue"] == "app-default"


class TestSQLAppTaskQueue:
    """Tests for BaseSQLMetadataExtractionApplication._task_queue."""

    def test_task_queue_with_deployment_name(self):
        with patch(
            "application_sdk.application.metadata_extraction.sql.DEPLOYMENT_NAME",
            "my-tenant",
        ):
            result = BaseSQLMetadataExtractionApplication._task_queue("snowflake")
        assert result == "atlan-snowflake-my-tenant"

    def test_task_queue_without_deployment_name(self):
        with patch(
            "application_sdk.application.metadata_extraction.sql.DEPLOYMENT_NAME",
            "",
        ):
            result = BaseSQLMetadataExtractionApplication._task_queue("snowflake")
        assert result == "snowflake"

    def test_task_queue_none_deployment_name(self):
        with patch(
            "application_sdk.application.metadata_extraction.sql.DEPLOYMENT_NAME",
            None,
        ):
            result = BaseSQLMetadataExtractionApplication._task_queue("postgres")
        assert result == "postgres"


class TestSQLAppGetManifest:
    """Tests for BaseSQLMetadataExtractionApplication.get_manifest."""

    def test_returns_none_when_no_workflow_class(self):
        """When _primary_workflow_class is not set, returns None (no contract file either)."""
        app = BaseSQLMetadataExtractionApplication("test-app")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.constants.CONTRACT_GENERATED_DIR", tmpdir):
                result = app.get_manifest()
        assert result is None

    def test_returns_hardcoded_dag_when_workflow_class_set(self):
        """When _primary_workflow_class is set but no contract file, returns hardcoded DAG."""
        app = BaseSQLMetadataExtractionApplication("test-app")
        app._primary_workflow_class = BaseSQLMetadataExtractionWorkflow

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("application_sdk.constants.CONTRACT_GENERATED_DIR", tmpdir),
                patch(
                    "application_sdk.application.metadata_extraction.sql.DEPLOYMENT_NAME",
                    "my-tenant",
                ),
                patch(
                    "application_sdk.application.metadata_extraction.sql.APPLICATION_NAME",
                    "snowflake",
                ),
            ):
                result = app.get_manifest()

        assert result is not None
        assert result["execution_mode"] == "automation-engine"
        assert "extract" in result["dag"]
        assert "publish" in result["dag"]

        extract = result["dag"]["extract"]
        assert extract["app_name"] == "snowflake"
        assert extract["inputs"]["workflow_type"] == "BaseSQLMetadataExtractionWorkflow"
        assert extract["inputs"]["app_name"] == "snowflake"
        assert extract["inputs"]["app_id"] == ""
        assert extract["inputs"]["argo_workflow_slug"] == ""
        assert extract["inputs"]["task_queue"] == "atlan-snowflake-my-tenant"

        publish = result["dag"]["publish"]
        assert publish["app_name"] == "publish"
        assert publish["inputs"]["workflow_type"] == "PublishWorkflow"
        assert publish["inputs"]["app_name"] == "publish"
        assert publish["inputs"]["task_queue"] == "atlan-publish-my-tenant"
        assert publish["depends_on"]["node_id"] == "extract"

    def test_contract_file_takes_priority_over_hardcoded_dag(self):
        """When app/generated/manifest.json exists, it wins over the hardcoded DAG."""
        app = BaseSQLMetadataExtractionApplication("test-app")
        app._primary_workflow_class = BaseSQLMetadataExtractionWorkflow

        contract_manifest = {"execution_mode": "custom", "dag": {"custom-node": {}}}

        with tempfile.TemporaryDirectory() as tmpdir:
            gen_dir = Path(tmpdir) / "app" / "generated"
            gen_dir.mkdir(parents=True)
            (gen_dir / "manifest.json").write_text(json.dumps(contract_manifest))

            with patch(
                "application_sdk.constants.CONTRACT_GENERATED_DIR", str(gen_dir)
            ):
                result = app.get_manifest()

        assert result["execution_mode"] == "custom"
        assert "custom-node" in result["dag"]

    def test_hardcoded_dag_has_correct_jsonpath_references(self):
        """Verify publish node args reference extract outputs via JSONPath."""
        app = BaseSQLMetadataExtractionApplication("test-app")
        app._primary_workflow_class = BaseSQLMetadataExtractionWorkflow

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("application_sdk.constants.CONTRACT_GENERATED_DIR", tmpdir),
                patch(
                    "application_sdk.application.metadata_extraction.sql.DEPLOYMENT_NAME",
                    "t",
                ),
                patch(
                    "application_sdk.application.metadata_extraction.sql.APPLICATION_NAME",
                    "app",
                ),
            ):
                result = app.get_manifest()

        publish_args = result["dag"]["publish"]["inputs"]["args"]
        assert (
            publish_args["connection_qualified_name"]
            == "$.extract.outputs.connection_qualified_name"
        )
        assert (
            publish_args["transformed_data_prefix"]
            == "$.extract.outputs.transformed_data_prefix"
        )
        assert (
            publish_args["publish_state_prefix"]
            == "$.extract.outputs.publish_state_prefix"
        )
        assert (
            publish_args["current_state_prefix"]
            == "$.extract.outputs.current_state_prefix"
        )

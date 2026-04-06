"""Unit tests for create_store_from_binding (GCS credential handling)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import orjson
import pytest
import yaml

from application_sdk.storage.binding import (
    GCS_SERVICE_ACCOUNT_FIELDS,
    create_store_from_binding,
)
from application_sdk.storage.errors import StorageConfigError


def _write_component(
    tmp_path: Path, name: str, binding_type: str, metadata: dict
) -> Path:
    """Write a Dapr component YAML and return the components dir."""
    components_dir = tmp_path / "components"
    components_dir.mkdir(exist_ok=True)
    doc = {
        "apiVersion": "dapr.io/v1alpha1",
        "kind": "Component",
        "metadata": {"name": name},
        "spec": {
            "type": binding_type,
            "metadata": [{"name": k, "value": v} for k, v in metadata.items()],
        },
    }
    (components_dir / f"{name}.yaml").write_text(yaml.dump(doc))
    return components_dir


# -- Fixtures for GCS service account metadata --

FULL_SA_META = {
    "bucket": "my-bucket",
    "type": "service_account",
    "project_id": "my-project",
    "private_key_id": "key-id-123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\\nMIIE...\\n-----END RSA PRIVATE KEY-----\\n",
    "client_email": "sa@my-project.iam.gserviceaccount.com",
    "client_id": "123456789",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/sa",
    "universe_domain": "googleapis.com",
}


class TestGCSStoreCredentials:
    """GCS branch of create_store_from_binding passes SA credentials correctly."""

    @patch("obstore.store.GCSStore")
    def test_full_sa_metadata_passed_as_service_account_key(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", FULL_SA_META
        )
        mock_gcs_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)

        mock_gcs_cls.assert_called_once()
        call_kwargs = mock_gcs_cls.call_args
        assert call_kwargs.kwargs["bucket"] == "my-bucket"

        config = call_kwargs.kwargs["config"]
        assert "service_account_key" in config
        sa_json = orjson.loads(config["service_account_key"])
        assert sa_json["project_id"] == "my-project"
        assert sa_json["client_email"] == "sa@my-project.iam.gserviceaccount.com"
        assert sa_json["universe_domain"] == "googleapis.com"

    @patch("obstore.store.GCSStore")
    def test_no_sa_fields_passes_empty_config(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """When no SA fields are present (local dev / ADC), config should be empty."""
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", {"bucket": "dev-bucket"}
        )
        mock_gcs_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)

        call_kwargs = mock_gcs_cls.call_args
        assert call_kwargs.kwargs["bucket"] == "dev-bucket"
        config = call_kwargs.kwargs["config"]
        assert config == {} or config is None or "service_account_key" not in config

    @patch("obstore.store.GCSStore")
    def test_partial_sa_fields_only_includes_present(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        partial_meta = {"bucket": "b", "project_id": "proj", "client_email": "e@x.com"}
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", partial_meta
        )
        mock_gcs_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)

        sa_json = orjson.loads(
            mock_gcs_cls.call_args.kwargs["config"]["service_account_key"]
        )
        assert set(sa_json.keys()) == {"project_id", "client_email"}

    @patch("obstore.store.GCSStore")
    def test_private_key_newlines_normalized(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Escaped \\n from Helm templating should become real newlines."""
        meta = {
            "bucket": "b",
            "type": "service_account",
            "private_key": "-----BEGIN KEY-----\\ndata\\n-----END KEY-----\\n",
        }
        components_dir = _write_component(tmp_path, "objectstore", "bindings.gcs", meta)
        mock_gcs_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)

        sa_json = orjson.loads(
            mock_gcs_cls.call_args.kwargs["config"]["service_account_key"]
        )
        assert "\\n" not in sa_json["private_key"]
        assert "\n" in sa_json["private_key"]

    @patch("obstore.store.GCSStore")
    def test_bindings_gcp_bucket_type_also_works(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Both 'bindings.gcs' and 'bindings.gcp.bucket' should route to GCS."""
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcp.bucket", {"bucket": "alt-bucket"}
        )
        mock_gcs_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)
        assert mock_gcs_cls.call_args.kwargs["bucket"] == "alt-bucket"


class TestGCSServiceAccountFieldsConstant:
    """Ensure the constant covers all standard GCP SA JSON fields."""

    def test_contains_required_fields(self) -> None:
        required = {
            "type",
            "project_id",
            "private_key_id",
            "private_key",
            "client_email",
            "client_id",
            "auth_uri",
            "token_uri",
            "auth_provider_x509_cert_url",
            "client_x509_cert_url",
            "universe_domain",
        }
        assert required == set(GCS_SERVICE_ACCOUNT_FIELDS)


class TestBindingErrors:
    def test_missing_component_raises(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        (components_dir / "empty.yaml").write_text(
            yaml.dump(
                {
                    "kind": "Component",
                    "metadata": {"name": "other"},
                    "spec": {"type": "bindings.gcs"},
                }
            )
        )

        with pytest.raises(StorageConfigError, match="No Dapr component named"):
            create_store_from_binding("nonexistent", components_dir=components_dir)

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.unknown", {}
        )

        with pytest.raises(StorageConfigError, match="Unsupported binding type"):
            create_store_from_binding("objectstore", components_dir=components_dir)

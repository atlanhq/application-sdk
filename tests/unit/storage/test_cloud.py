"""Unit tests for CloudStore."""

import json

import pytest

from application_sdk.storage.cloud import CloudStore, _infer_auth_type


class TestInferAuthType:
    def test_s3(self):
        assert _infer_auth_type({"s3_bucket": "my-bucket"}) == "s3"

    def test_gcs(self):
        assert _infer_auth_type({"gcs_bucket": "my-bucket"}) == "gcs"

    def test_adls_container(self):
        assert _infer_auth_type({"adls_container": "mycontainer"}) == "adls"

    def test_adls_account(self):
        assert _infer_auth_type({"storage_account_name": "myaccount"}) == "adls"

    def test_unknown(self):
        assert _infer_auth_type({}) == ""


class TestFromCredentials:
    def test_s3_explicit_auth_type(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket", "region": "us-east-1"},
            }
        )
        assert store.provider == "s3"

    def test_s3_inferred_auth_type(self):
        store = CloudStore.from_credentials(
            {
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_gcs(self):
        store = CloudStore.from_credentials(
            {
                "authType": "gcs",
                "extra": {"gcs_bucket": "test-bucket"},
            }
        )
        assert store.provider == "gcs"

    def test_adls(self):
        store = CloudStore.from_credentials(
            {
                "authType": "adls",
                "username": "client-id",
                "password": "client-secret",
                "extra": {
                    "storage_account_name": "myaccount",
                    "adls_container": "mycontainer",
                    "azure_tenant_id": "tenant-123",
                },
            }
        )
        assert store.provider == "adls"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Cannot determine cloud provider"):
            CloudStore.from_credentials({"username": "x", "password": "y"})

    def test_s3_missing_bucket_raises(self):
        with pytest.raises(ValueError, match="S3 bucket is required"):
            CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "AKID",
                    "password": "secret",
                    "extra": {},
                }
            )

    def test_gcs_missing_bucket_raises(self):
        with pytest.raises(ValueError, match="GCS bucket is required"):
            CloudStore.from_credentials(
                {
                    "authType": "gcs",
                    "extra": {},
                }
            )

    def test_adls_missing_account_raises(self):
        with pytest.raises(ValueError, match="Azure storage account is required"):
            CloudStore.from_credentials(
                {
                    "authType": "adls",
                    "extra": {},
                }
            )

    def test_extra_as_json_string(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": json.dumps({"s3_bucket": "test-bucket"}),
            }
        )
        assert store.provider == "s3"

    def test_extras_key_alias(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extras": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_auth_type_underscore(self):
        store = CloudStore.from_credentials(
            {
                "auth_type": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_s3_role_arn(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "extra": {
                    "s3_bucket": "test-bucket",
                    "aws_role_arn": "arn:aws:iam::123:role/MyRole",
                },
            }
        )
        assert store.provider == "s3"

    def test_adls_account_key_auth(self):
        import base64

        # Azure requires base64-encoded account keys
        fake_key = base64.b64encode(b"0" * 32).decode()
        store = CloudStore.from_credentials(
            {
                "authType": "adls",
                "password": fake_key,
                "extra": {
                    "storage_account_name": "myaccount",
                },
            }
        )
        assert store.provider == "adls"

    def test_store_property(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.store is not None

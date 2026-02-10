#!/usr/bin/env python3
"""
Centralized configuration for Iceberg/Polaris + AWS access.
Reads credentials from 'creds' file - update that file when credentials change.
"""

import os
from pathlib import Path

# ---------------------------------------
# Load credentials from 'creds' file
# ---------------------------------------
def _load_creds():
    """Load key=value pairs from creds file."""
    creds_file = Path(__file__).parent / "creds"
    creds = {}
    if creds_file.exists():
        with open(creds_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    creds[key.strip()] = value.strip()
    return creds

_creds = _load_creds()

# ---------------------------------------
# AWS Credentials
# Environment variables take precedence over creds file
# ---------------------------------------
AWS_REGION = os.environ.get("AWS_REGION") or _creds.get("AWS_REGION", "ap-south-1")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or _creds.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or _creds.get("AWS_SECRET_ACCESS_KEY", "")
# AWS_SESSION_TOKEN needs special handling - empty string in env should override creds file
AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN") if "AWS_SESSION_TOKEN" in os.environ else _creds.get("AWS_SESSION_TOKEN", "")

# ---------------------------------------
# S3 Source Configuration
# Environment variables take precedence over creds file
# ---------------------------------------
S3_BUCKET = os.environ.get("S3_BUCKET") or _creds.get("S3_BUCKET", "atlan-vcluster-atlan3zp01-274192nd53wj")
S3_PREFIX = os.environ.get("S3_PREFIX") or _creds.get("S3_PREFIX", "artifacts/apps/mssql/production/observability/logs")
# MinIO/S3 endpoint configuration (None for AWS S3, set for MinIO)
S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or _creds.get("S3_ENDPOINT")
S3_FORCE_PATH_STYLE = os.environ.get("S3_FORCE_PATH_STYLE", "false").lower() == "true" or _creds.get("S3_FORCE_PATH_STYLE", "false").lower() == "true"

# ---------------------------------------
# Polaris/Iceberg Catalog Configuration
# ---------------------------------------
POLARIS_CATALOG_URI = _creds.get("POLARIS_CATALOG_URI", "https://mdlh-aws.atlan.com/api/polaris/api/catalog")
POLARIS_CRED_READER = _creds.get("POLARIS_CRED_READER", "")
POLARIS_CRED_WRITER = _creds.get("POLARIS_CRED_WRITER", "")
POLARIS_CRED = POLARIS_CRED_WRITER  # Using writer creds by default
CATALOG_NAME = _creds.get("CATALOG_NAME", "context_store")
WAREHOUSE_NAME = _creds.get("WAREHOUSE_NAME", "context_store")

# ---------------------------------------
# Table Configuration
# ---------------------------------------
NAMESPACE = _creds.get("NAMESPACE", "Workflow_Log_Test")
ENTITY_TABLE = "workflow_logs_entity"
CHECKPOINT_TABLE = "workflow_logs_checkpoint"

# ---------------------------------------
# Processing Configuration
# ---------------------------------------
WORKER_THREADS = 1        # parallel download+normalize workers
BATCH_SIZE = 5            # files per Iceberg commit batch
COMMIT_RETRIES = 5        # retries for commit failures
COMMIT_RETRY_BASE = 1.0   # base seconds for exponential backoff


# ---------------------------------------
# Helper: Get fully qualified table name
# ---------------------------------------
def fq(table_name: str) -> str:
    """Return fully qualified table identifier: NAMESPACE.table_name"""
    return f"{NAMESPACE}.{table_name}"


# ---------------------------------------
# Helper: Setup AWS environment (for boto3)
# ---------------------------------------
def setup_aws_env():
    """Set AWS credentials in environment variables for boto3."""
    os.environ["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
    os.environ["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    os.environ["AWS_SESSION_TOKEN"] = AWS_SESSION_TOKEN
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION


# ---------------------------------------
# Helper: Get configured S3 client
# ---------------------------------------
def get_s3_client():
    """Return a boto3 S3 client with credentials from config."""
    import boto3
    from botocore.config import Config as BotoConfig

    client_kwargs = {
        "region_name": AWS_REGION,
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
    }

    # Only add session token if it's set (MinIO doesn't use it)
    if AWS_SESSION_TOKEN:
        client_kwargs["aws_session_token"] = AWS_SESSION_TOKEN

    # Add endpoint URL for MinIO or other S3-compatible storage
    if S3_ENDPOINT:
        client_kwargs["endpoint_url"] = S3_ENDPOINT

    # Configure path style if needed (required for MinIO)
    if S3_FORCE_PATH_STYLE:
        client_kwargs["config"] = BotoConfig(s3={"addressing_style": "path"})

    return boto3.client("s3", **client_kwargs)


# ---------------------------------------
# Helper: Load Polaris catalog
# ---------------------------------------
def get_catalog():
    """Return configured PyIceberg catalog."""
    from pyiceberg.catalog import load_catalog
    return load_catalog(
        CATALOG_NAME,
        **{
            "uri": POLARIS_CATALOG_URI,
            "credential": POLARIS_CRED,
            "warehouse": WAREHOUSE_NAME,
            "scope": "PRINCIPAL_ROLE:lake_writers",
        },
    )


# ---------------------------------------
# Debug: Print current config (without secrets)
# ---------------------------------------
def print_config():
    """Print current configuration (masks secrets)."""
    print(f"POLARIS_CATALOG_URI: {POLARIS_CATALOG_URI}")
    print(f"CATALOG_NAME: {CATALOG_NAME}")
    print(f"WAREHOUSE_NAME: {WAREHOUSE_NAME}")
    print(f"NAMESPACE: {NAMESPACE}")
    print(f"AWS_REGION: {AWS_REGION}")
    print(f"AWS_ACCESS_KEY_ID: {AWS_ACCESS_KEY_ID[:10]}..." if AWS_ACCESS_KEY_ID else "AWS_ACCESS_KEY_ID: (not set)")
    print(f"S3_BUCKET: {S3_BUCKET}")
    print(f"S3_PREFIX: {S3_PREFIX}")
    print(f"S3_ENDPOINT: {S3_ENDPOINT or '(AWS S3)'}")
    print(f"S3_FORCE_PATH_STYLE: {S3_FORCE_PATH_STYLE}")


if __name__ == "__main__":
    print("Current configuration:")
    print("-" * 40)
    print_config()

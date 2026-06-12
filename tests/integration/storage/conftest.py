"""Shared fixtures and helpers for storage integration tests.

Local-filesystem tests (``integration`` marker):
    Run against a real obstore ``LocalStore`` rooted in a pytest temp
    directory — no cloud credentials and no Temporal server needed:

    uv run pytest tests/integration/storage -m integration -v

Cloud-binding integration tests run against real cloud services and require
credentials passed via environment variables. All markers are deselected by
default via ``pyproject.toml``'s ``addopts``.

S3 tests (``s3_integration`` marker):
    export AWS_ACCESS_KEY_ID=<key>
    export AWS_SECRET_ACCESS_KEY=<secret>
    export AWS_DEFAULT_REGION=<region>     # default: us-east-1
    export S3_BUCKET=<existing-bucket>
    uv run pytest tests/integration/storage/test_binding_s3.py -m s3_integration -v

Azure tests (``azure_integration`` marker):
    export AZURE_STORAGE_ACCOUNT=<account-name>
    export AZURE_STORAGE_KEY=<account-key>
    export AZURE_STORAGE_CONTAINER=<existing-container>   # default: integ-test
    uv run pytest tests/integration/storage/test_binding_azure.py -m azure_integration -v

GCS tests (``gcs_integration`` marker):
    export GCS_BUCKET=<existing-bucket>
    export GCS_PROJECT_ID=<project-id>
    # For SA key test, also set:
    export GOOGLE_APPLICATION_CREDENTIALS=<path-to-sa-key.json>
    uv run pytest tests/integration/storage/test_binding_gcs.py -m gcs_integration -v
"""

from __future__ import annotations

import os

import pytest

from application_sdk import constants
from application_sdk.storage.factory import create_local_store

# ---------------------------------------------------------------------------
# Local-filesystem fixtures (no cloud credentials needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def local_store(tmp_path):
    """Real obstore ``LocalStore`` rooted in an isolated temp directory."""
    return create_local_store(tmp_path / "objectstore")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Staging directory wired up as the SDK's ``TEMPORARY_PATH``.

    v2-era callers pass workflow staging paths (``./local/tmp/...``) as
    object-store keys; ``normalize_key`` strips the staging root.  This
    fixture redirects the staging root to a temp directory so v2-style path
    inputs can be exercised hermetically.
    """
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setenv("ATLAN_TEMPORARY_PATH", str(staging_dir))
    monkeypatch.setattr(constants, "TEMPORARY_PATH", str(staging_dir))
    return staging_dir


# ---------------------------------------------------------------------------
# Real AWS S3 connection settings (env-var driven)
#
# AWS_ACCESS_KEY_ID      — access key (required)
# AWS_SECRET_ACCESS_KEY  — secret key (required)
# AWS_DEFAULT_REGION     — region (default: us-east-1)
# S3_BUCKET              — bucket that already exists (required)
# ---------------------------------------------------------------------------

S3_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID", "")
S3_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
S3_REGION = os.environ.get(
    "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")
)
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# ---------------------------------------------------------------------------
# Real Azure Blob Storage connection settings (env-var driven)
#
# AZURE_STORAGE_ACCOUNT  — storage account name (required)
# AZURE_STORAGE_KEY      — account key (required for accountKey auth tests)
# AZURE_STORAGE_CONTAINER — container that already exists; default "integ-test"
# ---------------------------------------------------------------------------

AZURE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
AZURE_KEY = os.environ.get("AZURE_STORAGE_KEY", "")
AZURE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "integ-test")

# ---------------------------------------------------------------------------
# Real GCS connection settings (env-var driven)
#
# GCS_BUCKET                     — bucket that already exists (required)
# GCS_PROJECT_ID                 — GCP project ID (required)
# GOOGLE_APPLICATION_CREDENTIALS — path to SA key JSON (for SA key test)
# ---------------------------------------------------------------------------

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
GCS_PROJECT_ID = os.environ.get("GCS_PROJECT_ID", "")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")


# ---------------------------------------------------------------------------
# Credential guards — autouse, marker-gated
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def require_s3(request):
    """Skip tests marked ``s3_integration`` when AWS credentials are absent."""
    if not request.node.get_closest_marker("s3_integration"):
        return
    missing = []
    if not S3_ACCESS_KEY:
        missing.append("AWS_ACCESS_KEY_ID")
    if not S3_SECRET_KEY:
        missing.append("AWS_SECRET_ACCESS_KEY")
    if not S3_BUCKET:
        missing.append("S3_BUCKET")
    if missing:
        pytest.skip(
            f"Real AWS credentials required. Missing: {', '.join(missing)}. "
            "Also set AWS_DEFAULT_REGION and S3_BUCKET."
        )


@pytest.fixture(autouse=True)
def require_azure(request):
    """Skip tests marked ``azure_integration`` when Azure credentials are absent."""
    if not request.node.get_closest_marker("azure_integration"):
        return
    if not AZURE_ACCOUNT or not AZURE_KEY:
        pytest.skip(
            "Real Azure credentials required. Set AZURE_STORAGE_ACCOUNT and AZURE_STORAGE_KEY. "
            "Optionally set AZURE_STORAGE_CONTAINER (default: integ-test)."
        )


@pytest.fixture(autouse=True)
def require_gcs(request):
    """Skip tests marked ``gcs_integration`` when GCS credentials are absent."""
    if not request.node.get_closest_marker("gcs_integration"):
        return
    if not GCS_BUCKET or not GCS_PROJECT_ID:
        pytest.skip(
            "Real GCS credentials required. Set GCS_BUCKET and GCS_PROJECT_ID. "
            "Also set GOOGLE_APPLICATION_CREDENTIALS for service-account-key tests."
        )


# ---------------------------------------------------------------------------
# Component YAML helper
# ---------------------------------------------------------------------------


def write_dapr_component(
    components_dir: object,
    name: str,
    binding_type: str,
    metadata: dict[str, str],
) -> None:
    """Write a Dapr Component YAML into *components_dir*.

    Args:
        components_dir: ``pathlib.Path`` to the components directory.
        name: Dapr component ``metadata.name`` value.
        binding_type: ``spec.type`` (e.g. ``"bindings.aws.s3"``).
        metadata: Flat dict of metadata key → value pairs.
    """
    from pathlib import Path

    d = Path(str(components_dir))
    d.mkdir(parents=True, exist_ok=True)

    meta_lines = "\n".join(
        f"    - name: {k}\n      value: {v!r}" for k, v in metadata.items()
    )
    yaml_text = f"""\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: {name}
spec:
  version: v1
  type: {binding_type}
  metadata:
{meta_lines}
"""
    (d / f"{name}.yaml").write_text(yaml_text)

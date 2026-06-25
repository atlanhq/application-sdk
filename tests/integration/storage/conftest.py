"""Shared fixtures and helpers for storage integration tests.

Local-filesystem tests (``integration`` marker):
    Run against a real obstore ``LocalStore`` rooted in a pytest temp
    directory — no cloud credentials and no Temporal server needed:

    uv run pytest tests/integration/storage -m integration -v

Cloud-binding integration tests run against real cloud services using KEYLESS
auth (GitHub OIDC in CI — no static secrets). The binding carries no embedded
credentials; the SDK uses the ambient/federated credential the runtime provides
(the production SDR / customer-infra path). Embedded-credential resolution
(static keys, account keys, SA-key JSON) is covered hermetically by the
``test_emulator_*`` tests. All markers are deselected by default via
``pyproject.toml``'s ``addopts``.

S3 tests (``s3_integration`` marker) — ambient AWS credential chain:
    # ambient AWS creds on the env (OIDC role in CI; any valid chain locally) +
    export S3_BUCKET=<existing-bucket>
    export AWS_DEFAULT_REGION=<region>     # default: us-east-1
    uv run pytest tests/integration/storage/test_binding_s3.py -m s3_integration -v

Azure tests (``azure_integration`` marker) — Workload Identity Federation:
    export AZURE_STORAGE_ACCOUNT=<account-name>
    export AZURE_STORAGE_CONTAINER=<existing-container>   # default: integ-test
    export AZURE_CLIENT_ID=<workload-identity-app-id>
    export AZURE_TENANT_ID=<tenant-id>
    export AZURE_FEDERATED_TOKEN_FILE=<path-to-oidc-token-file>
    uv run pytest tests/integration/storage/test_binding_azure.py -m azure_integration -v

GCS tests (``gcs_integration`` marker) — Application Default Credentials:
    export GCS_BUCKET=<existing-bucket>
    export GCS_PROJECT_ID=<project-id>
    export GOOGLE_APPLICATION_CREDENTIALS=<ADC / WIF credential file>
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
# Keyless (Workload Identity Federation) — the CI default. No account key; obstore
# exchanges the federated token file using the SP client/tenant id.
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
AZURE_FEDERATED_TOKEN_FILE = os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "")

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
    """Skip ``s3_integration`` tests unless keyless AWS creds are present.

    Keyless: the CI OIDC step (configure-aws-credentials) exports the ambient
    chain (AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN); tests omit creds from the
    binding so obstore uses that chain. We gate on S3_BUCKET + an ambient key.
    """
    if not request.node.get_closest_marker("s3_integration"):
        return
    missing = []
    if not S3_BUCKET:
        missing.append("S3_BUCKET")
    if not S3_ACCESS_KEY:  # AWS_ACCESS_KEY_ID — ambient creds from the OIDC step
        missing.append("ambient AWS credentials (AWS_ACCESS_KEY_ID)")
    if missing:
        pytest.skip(
            f"Real AWS access required. Missing: {', '.join(missing)}. "
            "In CI these come from the keyless OIDC role; locally set "
            "S3_BUCKET + AWS creds (and AWS_DEFAULT_REGION)."
        )


@pytest.fixture(autouse=True)
def require_azure(request):
    """Skip ``azure_integration`` tests unless keyless Azure WI creds are present.

    Keyless: the storage account has shared-key access disabled, so auth is
    Workload Identity Federation — the SP client/tenant id plus a federated
    token file (the GitHub OIDC token) that obstore exchanges for an AAD token.
    """
    if not request.node.get_closest_marker("azure_integration"):
        return
    missing = []
    if not AZURE_ACCOUNT:
        missing.append("AZURE_STORAGE_ACCOUNT")
    if not AZURE_CLIENT_ID:
        missing.append("AZURE_CLIENT_ID")
    if not AZURE_TENANT_ID:
        missing.append("AZURE_TENANT_ID")
    if not AZURE_FEDERATED_TOKEN_FILE or not os.path.exists(AZURE_FEDERATED_TOKEN_FILE):
        missing.append("AZURE_FEDERATED_TOKEN_FILE (existing)")
    if missing:
        pytest.skip(
            f"Real Azure (Workload Identity) access required. Missing: {', '.join(missing)}. "
            "In CI these come from the keyless OIDC federated SP."
        )


@pytest.fixture(autouse=True)
def require_gcs(request):
    """Skip ``gcs_integration`` tests unless keyless GCS (ADC) access is present.

    Keyless: the CI WIF step (google-github-actions/auth) sets ADC via
    GOOGLE_APPLICATION_CREDENTIALS; tests omit creds so obstore uses ADC.
    """
    if not request.node.get_closest_marker("gcs_integration"):
        return
    missing = []
    if not GCS_BUCKET:
        missing.append("GCS_BUCKET")
    if not GCS_PROJECT_ID:
        missing.append("GCS_PROJECT_ID")
    if not GOOGLE_APPLICATION_CREDENTIALS:  # ADC file from the WIF step
        missing.append("GOOGLE_APPLICATION_CREDENTIALS (ADC)")
    if missing:
        pytest.skip(
            f"Real GCS access required. Missing: {', '.join(missing)}. "
            "In CI these come from the keyless WIF auth step."
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

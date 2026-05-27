"""Shared fixtures and helpers for storage integration tests.

Prerequisites:
    docker compose -f tests/integration/storage/docker-compose.yml up -d

S3 (MinIO) tests require the ``s3_integration`` marker.
Azure (Azurite) tests require the ``azure_integration`` marker.
Both markers are deselected by default via ``pyproject.toml``'s ``addopts``.
"""

from __future__ import annotations

import os
import socket

import pytest

# ---------------------------------------------------------------------------
# MinIO connection settings (all overridable via env vars)
# ---------------------------------------------------------------------------

MINIO_HOST = os.environ.get("MINIO_HOST", "localhost")
MINIO_PORT = int(os.environ.get("MINIO_PORT", "9000"))
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio123")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "test-bucket")
MINIO_ENDPOINT = f"http://{MINIO_HOST}:{MINIO_PORT}"

# ---------------------------------------------------------------------------
# Azurite connection settings
# ---------------------------------------------------------------------------

AZURITE_HOST = os.environ.get("AZURITE_HOST", "localhost")
AZURITE_PORT = int(os.environ.get("AZURITE_PORT", "10000"))
AZURITE_ACCOUNT = "devstoreaccount1"
# Well-known Azurite development storage account key (not a real credential)
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KkM1ufWxYZgw=="
AZURITE_CONTAINER = os.environ.get("AZURITE_CONTAINER", "test-container")
AZURITE_BLOB_ENDPOINT = f"http://{AZURITE_HOST}:{AZURITE_PORT}/{AZURITE_ACCOUNT}"


def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Connectivity guards — autouse, marker-gated
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def require_minio(request):
    """Skip tests marked ``s3_integration`` when MinIO is not reachable."""
    if not request.node.get_closest_marker("s3_integration"):
        return
    if not _is_port_open(MINIO_HOST, MINIO_PORT):
        pytest.skip(
            f"MinIO not running on {MINIO_HOST}:{MINIO_PORT}. "
            "Start it with: docker compose -f tests/integration/storage/docker-compose.yml up -d minio minio-setup"
        )


@pytest.fixture(autouse=True)
def require_azurite(request):
    """Skip tests marked ``azure_integration`` when Azurite is not reachable."""
    if not request.node.get_closest_marker("azure_integration"):
        return
    if not _is_port_open(AZURITE_HOST, AZURITE_PORT):
        pytest.skip(
            f"Azurite not running on {AZURITE_HOST}:{AZURITE_PORT}. "
            "Start it with: docker compose -f tests/integration/storage/docker-compose.yml up -d azurite"
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

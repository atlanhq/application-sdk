"""Pydantic models for YAML-driven datasource fixture configuration."""

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


class ReadinessConfig(BaseModel):
    """Configuration for container readiness checks."""

    strategy: Literal["tcp"] = "tcp"
    timeout: float = 60.0
    interval: float = 2.0


class VolumeConfig(BaseModel):
    """A host-path to container-path volume mount."""

    host_path: str
    container_path: str
    mode: str = "ro"


class ContainerizedDatasourceConfig(BaseModel):
    """YAML config for a testcontainers-backed data source."""

    type: Literal["containerized"] = "containerized"
    image: str = Field(description="Docker image name:tag, e.g. 'postgres:15.12'")
    port: int = Field(description="Container port to expose, e.g. 5432")
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables passed to the container",
    )
    volumes: List[VolumeConfig] = Field(
        default_factory=list, description="Volume mounts for seed files"
    )
    credentials: Dict[str, Any] = Field(
        default_factory=dict,
        description="Credential fields passed through to tests. Keys are UPPERCASED "
        "when generating env vars (e.g. username -> E2E_POSTGRES_USERNAME).",
    )
    env_prefix: str = Field(
        description="Prefix for auto-generated env vars, e.g. 'E2E_POSTGRES'"
    )
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)

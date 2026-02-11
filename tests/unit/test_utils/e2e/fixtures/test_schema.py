import pytest
from pydantic import ValidationError

from application_sdk.test_utils.e2e.fixtures.schema import (
    ContainerizedDatasourceConfig,
    ReadinessConfig,
    VolumeConfig,
)


class TestReadinessConfig:
    def test_defaults(self):
        cfg = ReadinessConfig()
        assert cfg.strategy == "tcp"
        assert cfg.timeout == 60.0
        assert cfg.interval == 2.0

    def test_custom_values(self):
        cfg = ReadinessConfig(strategy="tcp", timeout=30.0, interval=1.0)
        assert cfg.timeout == 30.0
        assert cfg.interval == 1.0


class TestVolumeConfig:
    def test_defaults(self):
        vol = VolumeConfig(host_path="./seed.sql", container_path="/init/seed.sql")
        assert vol.mode == "ro"

    def test_custom_mode(self):
        vol = VolumeConfig(host_path="./data", container_path="/data", mode="rw")
        assert vol.mode == "rw"


class TestContainerizedDatasourceConfig:
    def test_minimal_config(self):
        cfg = ContainerizedDatasourceConfig(
            image="postgres:15",
            port=5432,
            env_prefix="E2E_PG",
        )
        assert cfg.type == "containerized"
        assert cfg.image == "postgres:15"
        assert cfg.port == 5432
        assert cfg.env == {}
        assert cfg.volumes == []
        assert cfg.credentials == {}
        assert cfg.readiness.strategy == "tcp"

    def test_full_config(self):
        cfg = ContainerizedDatasourceConfig(
            image="postgres:15.12",
            port=5432,
            env={"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "pass"},
            volumes=[
                VolumeConfig(
                    host_path="./seed.sql",
                    container_path="/docker-entrypoint-initdb.d/seed.sql",
                )
            ],
            credentials={"username": "postgres", "password": "pass", "database": "db"},
            env_prefix="E2E_POSTGRES",
            readiness=ReadinessConfig(timeout=30.0),
        )
        assert len(cfg.volumes) == 1
        assert cfg.credentials["username"] == "postgres"
        assert cfg.readiness.timeout == 30.0

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            ContainerizedDatasourceConfig()  # type: ignore[call-arg]

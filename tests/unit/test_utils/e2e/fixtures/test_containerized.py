from pathlib import Path
from unittest.mock import MagicMock, patch

from application_sdk.test_utils.e2e.fixtures.containerized import ContainerizedFixture
from application_sdk.test_utils.e2e.fixtures.schema import (
    ContainerizedDatasourceConfig,
    ReadinessConfig,
    VolumeConfig,
)


def _make_config(**overrides) -> ContainerizedDatasourceConfig:
    defaults = {
        "type": "containerized",
        "image": "postgres:15.12",
        "port": 5432,
        "env": {"POSTGRES_USER": "pg", "POSTGRES_PASSWORD": "pass"},
        "volumes": [
            VolumeConfig(
                host_path="./seed.sql",
                container_path="/docker-entrypoint-initdb.d/seed.sql",
            )
        ],
        "credentials": {"username": "pg", "password": "pass", "database": "testdb"},
        "env_prefix": "E2E_POSTGRES",
        "readiness": ReadinessConfig(timeout=5.0, interval=0.1),
    }
    defaults.update(overrides)
    return ContainerizedDatasourceConfig(**defaults)


def _make_mock_container():
    container = MagicMock()
    container.get_container_host_ip.return_value = "localhost"
    container.get_exposed_port.return_value = "54321"
    return container


class TestContainerizedFixture:
    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_setup_creates_container_and_returns_connection_info(
        self, mock_docker_cls, mock_tcp
    ):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container
        mock_container.with_volume_mapping.return_value = mock_container

        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))

        info = fixture.setup()

        assert info.host == "localhost"
        assert info.port == 54321
        assert info.credentials["username"] == "pg"
        assert info.credentials["password"] == "pass"
        assert info.credentials["database"] == "testdb"
        mock_container.start.assert_called_once()

    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_get_env_vars_uses_prefix(self, mock_docker_cls, mock_tcp):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container
        mock_container.with_volume_mapping.return_value = mock_container

        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        fixture.setup()

        env_vars = fixture.get_env_vars()
        assert env_vars["E2E_POSTGRES_HOST"] == "localhost"
        assert env_vars["E2E_POSTGRES_PORT"] == "54321"
        assert env_vars["E2E_POSTGRES_USERNAME"] == "pg"
        assert env_vars["E2E_POSTGRES_PASSWORD"] == "pass"
        assert env_vars["E2E_POSTGRES_DATABASE"] == "testdb"

    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_teardown_stops_container(self, mock_docker_cls, mock_tcp):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container
        mock_container.with_volume_mapping.return_value = mock_container

        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        fixture.setup()
        fixture.teardown()

        mock_container.stop.assert_called_once()
        assert fixture._container is None
        assert fixture._connection_info is None

    def test_get_env_vars_empty_before_setup(self):
        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        assert fixture.get_env_vars() == {}

    def test_is_ready_false_before_setup(self):
        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        assert fixture.is_ready() is False

    def test_resolve_volume_path_relative(self, tmp_path):
        config = _make_config()
        fixture = ContainerizedFixture(config, yaml_dir=tmp_path)
        resolved = fixture._resolve_volume_path("./seed.sql")
        assert resolved == (tmp_path / "seed.sql").resolve()

    def test_resolve_volume_path_absolute(self, tmp_path):
        config = _make_config()
        abs_path = tmp_path / "seed.sql"
        fixture = ContainerizedFixture(config, yaml_dir=Path("/unused"))
        resolved = fixture._resolve_volume_path(str(abs_path))
        assert resolved == abs_path

    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_setup_with_no_volumes(self, mock_docker_cls, mock_tcp):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container

        config = _make_config(volumes=[])
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        info = fixture.setup()

        assert info.host == "localhost"
        mock_container.with_volume_mapping.assert_not_called()

    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_all_credentials_passed_through(self, mock_docker_cls, mock_tcp):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container
        mock_container.with_volume_mapping.return_value = mock_container

        config = _make_config(
            credentials={
                "username": "pg",
                "password": "pass",
                "database": "testdb",
                "api_token": "tok123",
            }
        )
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        info = fixture.setup()

        assert info.credentials == {
            "username": "pg",
            "password": "pass",
            "database": "testdb",
            "api_token": "tok123",
        }

    @patch(
        "application_sdk.test_utils.e2e.fixtures.containerized.check_tcp",
        return_value=True,
    )
    @patch("testcontainers.core.container.DockerContainer")
    def test_get_env_vars_uppercases_credential_keys(self, mock_docker_cls, mock_tcp):
        mock_container = _make_mock_container()
        mock_docker_cls.return_value = mock_container
        mock_container.with_exposed_ports.return_value = mock_container
        mock_container.with_env.return_value = mock_container
        mock_container.with_volume_mapping.return_value = mock_container

        config = _make_config(
            credentials={"username": "pg", "api_token": "tok123"},
            env_prefix="E2E_SUPER",
        )
        fixture = ContainerizedFixture(config, yaml_dir=Path("/project/tests"))
        fixture.setup()

        env_vars = fixture.get_env_vars()
        assert env_vars["E2E_SUPER_USERNAME"] == "pg"
        assert env_vars["E2E_SUPER_API_TOKEN"] == "tok123"
        assert env_vars["E2E_SUPER_HOST"] == "localhost"
        assert env_vars["E2E_SUPER_PORT"] == "54321"

import pytest

from application_sdk.test_utils.e2e.fixtures.containerized import ContainerizedFixture
from application_sdk.test_utils.e2e.fixtures.loader import load_fixture_from_yaml


class TestLoadFixtureFromYaml:
    def test_loads_containerized_config(self, tmp_path):
        yaml_file = tmp_path / "datasource.yaml"
        yaml_file.write_text(
            """
image: postgres:15.12
port: 5432
env:
  POSTGRES_USER: postgres
  POSTGRES_PASSWORD: postgres
env_prefix: E2E_PG
credentials:
  username: postgres
  password: postgres
  database: testdb
"""
        )
        fixture = load_fixture_from_yaml(yaml_file)
        assert isinstance(fixture, ContainerizedFixture)

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError, match="Datasource config not found"):
            load_fixture_from_yaml("/nonexistent/datasource.yaml")

    def test_raises_on_invalid_yaml(self, tmp_path):
        yaml_file = tmp_path / "datasource.yaml"
        yaml_file.write_text("invalid: true\n")
        with pytest.raises(Exception):
            load_fixture_from_yaml(yaml_file)

    def test_raises_on_missing_required_fields(self, tmp_path):
        yaml_file = tmp_path / "datasource.yaml"
        yaml_file.write_text("image: postgres:15\n")
        with pytest.raises(Exception):
            load_fixture_from_yaml(yaml_file)

    def test_with_volumes_and_readiness(self, tmp_path):
        yaml_file = tmp_path / "datasource.yaml"
        yaml_file.write_text(
            """
image: mysql:8
port: 3306
env_prefix: E2E_MYSQL
volumes:
  - host_path: ./seed.sql
    container_path: /docker-entrypoint-initdb.d/seed.sql
    mode: ro
credentials:
  username: root
  password: root
  database: testdb
readiness:
  strategy: tcp
  timeout: 30
  interval: 1
"""
        )
        fixture = load_fixture_from_yaml(yaml_file)
        assert isinstance(fixture, ContainerizedFixture)
        assert fixture._config.readiness.timeout == 30.0
        assert len(fixture._config.volumes) == 1

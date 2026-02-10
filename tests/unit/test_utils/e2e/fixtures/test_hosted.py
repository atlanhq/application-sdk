import pytest

from application_sdk.test_utils.e2e.fixtures.hosted import HostedDataSourceFixture


class SampleHostedFixture(HostedDataSourceFixture):
    """Generic hosted fixture for testing."""

    def get_required_env_vars(self) -> dict:
        return {
            "host": "E2E_DS_HOST",
            "port": "E2E_DS_PORT",
            "username": "E2E_DS_USER",
            "password": "E2E_DS_PASS",
            "database": "E2E_DS_DB",
            "warehouse": "E2E_DS_WAREHOUSE",
        }


class TestHostedDataSourceFixture:
    def test_setup_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("E2E_DS_HOST", "datasource.example.com")
        monkeypatch.setenv("E2E_DS_PORT", "443")
        monkeypatch.setenv("E2E_DS_USER", "admin")
        monkeypatch.setenv("E2E_DS_PASS", "secret")
        monkeypatch.setenv("E2E_DS_DB", "analytics")
        monkeypatch.setenv("E2E_DS_WAREHOUSE", "compute_wh")

        fixture = SampleHostedFixture()
        info = fixture.setup()

        assert info.host == "datasource.example.com"
        assert info.port == 443
        assert info.username == "admin"
        assert info.database == "analytics"
        assert info.extra == {"warehouse": "compute_wh"}

    def test_setup_raises_on_missing_env_vars(self, monkeypatch):
        monkeypatch.setenv("E2E_DS_HOST", "host")
        monkeypatch.delenv("E2E_DS_PORT", raising=False)
        monkeypatch.delenv("E2E_DS_USER", raising=False)
        monkeypatch.delenv("E2E_DS_PASS", raising=False)
        monkeypatch.delenv("E2E_DS_DB", raising=False)
        monkeypatch.delenv("E2E_DS_WAREHOUSE", raising=False)

        fixture = SampleHostedFixture()
        with pytest.raises(EnvironmentError, match="Missing required environment"):
            fixture.setup()

    def test_teardown_is_noop(self):
        fixture = SampleHostedFixture()
        fixture.teardown()  # should not raise

    def test_is_ready_returns_true(self):
        fixture = SampleHostedFixture()
        assert fixture.is_ready() is True

    def test_get_env_vars_returns_empty(self):
        fixture = SampleHostedFixture()
        assert fixture.get_env_vars() == {}

    def test_default_required_env_vars_is_empty(self):
        fixture = HostedDataSourceFixture()
        assert fixture.get_required_env_vars() == {}

    def test_setup_with_no_required_vars(self):
        fixture = HostedDataSourceFixture()
        info = fixture.setup()
        assert info.host == ""
        assert info.port == 0

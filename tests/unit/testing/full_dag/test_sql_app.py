"""Unit tests for SQLAppE2EFullTest — the SQL-connector convenience base.

Network-free. The pyatlan ``$admin`` resolution is the only external
dependency and is monkey-patched out via ``_resolve_admin_role_guid``.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.full_dag import RunMode, SQLAppE2EFullTest
from application_sdk.testing.full_dag.payload import AgentSpec, DatabaseSpec


def _make_test(role_guid: str = "stub-admin-guid"):
    """Build a runnable subclass with required attrs + stubbed pyatlan."""

    class _Concrete(SQLAppE2EFullTest):
        connector_short_name = "mysql"
        argo_package_name = "@atlan/mysql"
        argo_template_name = "atlan-mysql"
        mode = RunMode.AGENT
        app_service_url = "http://mysql.mysql-app.svc.cluster.local"

    # Stub the pyatlan role-cache lookup so tests stay network-free
    _Concrete._resolve_admin_role_guid = staticmethod(lambda: role_guid)

    def database_spec(self):
        return DatabaseSpec(host="mysql", port=3306, username="u", password="p")

    _Concrete.database_spec = database_spec
    return _Concrete


def _bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
    monkeypatch.setenv("ATLAN_API_KEY", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "9999999")


def test_agent_spec_agent_mode_uses_run_id_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT mode produces ``{connector}-{prefix}-{run_id}`` agent name."""
    _bootstrap_env(monkeypatch)
    cls = _make_test()
    instance = cls()
    instance.setup_method()
    agent = instance.agent_spec()
    assert isinstance(agent, AgentSpec)
    # Default connection_name_prefix = "e2e-full-ci"; run_id = 9999999.
    assert agent.agent_name == "mysql-e2e-full-ci-9999999"


def test_agent_spec_direct_mode_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIRECT mode produces no AgentSpec — worker reads creds inline."""
    _bootstrap_env(monkeypatch)
    cls = _make_test()
    cls.mode = RunMode.DIRECT
    instance = cls()
    instance.setup_method()
    assert instance.agent_spec() is None


def test_agent_name_template_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subclasses can override the agent_name_template for non-standard
    Argo cluster templates."""
    _bootstrap_env(monkeypatch)
    cls = _make_test()
    cls.agent_name_template = "{prefix}-{connector}-{run_id}"
    instance = cls()
    instance.setup_method()
    agent = instance.agent_spec()
    assert agent is not None
    assert agent.agent_name == "e2e-full-ci-mysql-9999999"


def test_connection_spec_resolves_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connection_spec`` injects the resolved ``$admin`` role GUID."""
    _bootstrap_env(monkeypatch)
    cls = _make_test(role_guid="role-guid-xyz")
    instance = cls()
    instance.setup_method()
    spec = instance.connection_spec()
    assert spec.admin_roles == ("role-guid-xyz",)
    assert spec.connector_name == "mysql"
    assert spec.qualified_name.startswith("default/mysql/e2e-full-ci-")
    assert spec.source_logo.endswith("/mysql.png")


def test_connection_spec_caches_role_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role-cache lookup happens once even across multiple calls."""
    _bootstrap_env(monkeypatch)
    calls = {"n": 0}

    def stub_lookup() -> str:
        calls["n"] += 1
        return "cached-role-guid"

    cls = _make_test()
    cls._resolve_admin_role_guid = staticmethod(stub_lookup)
    instance = cls()
    instance.setup_method()
    instance.connection_spec()
    instance.connection_spec()
    instance.connection_spec()
    assert calls["n"] == 1


def test_connection_admin_users_and_groups_are_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subclass-set adminUsers/Groups land on the resulting ConnectionSpec."""
    _bootstrap_env(monkeypatch)
    cls = _make_test()
    cls.connection_admin_users = ("dev-team",)
    cls.connection_admin_groups = ("analytics",)
    instance = cls()
    instance.setup_method()
    spec = instance.connection_spec()
    assert spec.admin_users == ("dev-team",)
    assert spec.admin_groups == ("analytics",)

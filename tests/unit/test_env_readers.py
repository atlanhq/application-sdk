"""The single readers in :mod:`application_sdk._env` for variables the SDK
once defaulted differently per module (FND-4265), and a guard that keeps
each to its one reader."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from application_sdk import _env

_PACKAGE = Path(__file__).resolve().parents[2] / "application_sdk"


class TestDeploymentName:
    def test_unset_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
        assert _env.deployment_name() == "local"

    def test_blank_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "  ")
        assert _env.deployment_name() == "local"

    def test_set_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", " prod ")
        assert _env.deployment_name() == "prod"


class TestDaprComponentsPath:
    def test_unset_is_dot_components(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DAPR_COMPONENTS_PATH", raising=False)
        assert _env.dapr_components_path() == "./components"

    def test_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DAPR_COMPONENTS_PATH", "/app/components")
        assert _env.dapr_components_path() == "/app/components"


class TestPodName:
    @pytest.mark.parametrize(
        ("k8s", "hostname", "expected"),
        [
            ("pod-a", "host-b", "pod-a"),
            (None, "host-b", "host-b"),
            ("", "host-b", "host-b"),
            (None, None, ""),
        ],
    )
    def test_precedence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        k8s: str | None,
        hostname: str | None,
        expected: str,
    ) -> None:
        for key, value in (("K8S_POD_NAME", k8s), ("HOSTNAME", hostname)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        assert _env.pod_name() == expected


class TestLegacyWorkflowHost:
    """The v2 Temporal address, one reader for AppConfig and ExecutionSettings (FND-4095)."""

    @pytest.mark.parametrize(
        ("host", "port", "expected"),
        [
            (None, None, ""),
            ("  ", "7236", ""),
            ("temporal.internal", None, "temporal.internal:7233"),
            ("temporal.internal", "", "temporal.internal:7233"),
            ("temporal.internal", "7236", "temporal.internal:7236"),
        ],
    )
    def test_host_and_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        host: str | None,
        port: str | None,
        expected: str,
    ) -> None:
        for key, value in (
            ("ATLAN_WORKFLOW_HOST", host),
            ("ATLAN_WORKFLOW_PORT", port),
        ):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        assert _env.legacy_workflow_host() == expected


#: Variable -> the modules allowed to name it in a call. ``_env`` holds the
#: one reader; ``common/task_queue`` reads ``ATLAN_DEPLOYMENT_NAME`` raw for
#: queue derivation, where unset means "no prefix", not a default.
_ONE_READER: dict[str, frozenset[str]] = {
    "ATLAN_DEPLOYMENT_NAME": frozenset({"_env.py", "common/task_queue.py"}),
    "DAPR_COMPONENTS_PATH": frozenset({"_env.py"}),
    "K8S_POD_NAME": frozenset({"_env.py"}),
    "HOSTNAME": frozenset({"_env.py"}),
    "ATLAN_WORKFLOW_HOST": frozenset({"_env.py"}),
    "ATLAN_WORKFLOW_PORT": frozenset({"_env.py"}),
}
#: Code that sets these for a child process or a test, not reads them.
_EXCLUDED_DIRS = ("testing/", "dev/")


def _readers(name: str) -> set[str]:
    found: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        rel = path.relative_to(_PACKAGE).as_posix()
        if rel.startswith(_EXCLUDED_DIRS):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            # ``app_env(app, "ATLAN_DEPLOYMENT_NAME")`` reads the app-scoped
            # ``ATLAN_APPENV__<APP>__...`` variable, a different variable.
            func = node.func
            if isinstance(func, ast.Name) and func.id == "app_env":
                continue
            if any(
                isinstance(arg, ast.Constant) and arg.value == name for arg in node.args
            ):
                found.add(rel)
    return found


@pytest.mark.parametrize("name", sorted(_ONE_READER))
def test_each_variable_has_one_reader(name: str) -> None:
    """Read anywhere else, a variable grows a second default (FND-4265)."""
    assert _readers(name) <= _ONE_READER[name]
